#!/usr/bin/env python3
"""Montatore v2 della Regia di A' Villetta (gira sul motore GitHub; in locale per i test).

Esegue il PIANO DI MONTAGGIO scritto dagli agenti (regista, copy, audio, colorista, motion designer):
- tre stili: "ritmo" (tagli sul battito, zoom e rallenty), "cinematico" (inquadrature lunghe,
  dissolvenze, bagliori, bande cinema), "parlato" (voce/avatar con sottotitoli parola per parola
  e immagini di copertura)
- musica: battiti trovati in automatico (librosa) -> i tagli cadono a tempo; voce sopra la musica
- colore: normalizzazione di ogni clip + look (LUT generata) uguale su tutto il video
- effetti: zoom sul battito, speed ramp, tremolio di camera, transizioni a scia/zoom/flash,
  bagliori di luce, grana, vignettatura, bande cinema
- testi: titolo animato, scritte, sottotitoli parola per parola (Whisper sulla voce)
Solo ffmpeg + libreria standard; librosa e faster-whisper sono facoltativi (se mancano: griglia fissa).
"""
import json, os, subprocess, sys, tempfile, urllib.request, shutil, math, random

REGIA = os.environ.get("REGIA", "https://avilletta-regia.gestionalelcl.workers.dev")
JOB = os.environ.get("JOB", "")
PASS = os.environ.get("PASS", "")
W, H, FPS = 1080, 1920, 30
TMP = tempfile.mkdtemp(prefix="monta_")
FONTDIR = os.path.join(TMP, "fonts")
os.makedirs(FONTDIR, exist_ok=True)
FONT_URLS = {
    "Great Vibes": "https://raw.githubusercontent.com/google/fonts/main/ofl/greatvibes/GreatVibes-Regular.ttf",
    "Montserrat": "https://raw.githubusercontent.com/google/fonts/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
    "Playfair Display": "https://raw.githubusercontent.com/google/fonts/main/ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf",
}
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS)]


def log(*a):
    print("[montaggio]", *a, flush=True)


# ------------------------------------------------------------------ rete
def regia(path, data=None, ctype="application/json"):
    req = urllib.request.Request(REGIA + path, data=data, method="POST" if data is not None else "GET")
    req.add_header("x-montaggio", PASS)
    req.add_header("user-agent", "villetta-montaggio/2.0")
    if data is not None:
        req.add_header("content-type", ctype)
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def scarica(url, dest):
    if url.startswith("/"):
        url = REGIA + url
    req = urllib.request.Request(url, headers={"user-agent": "Mozilla/5.0 villetta-montaggio/2.0", "x-montaggio": PASS})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def prepara_font():
    for nome, url in FONT_URLS.items():
        p = os.path.join(FONTDIR, nome.replace(" ", "") + ".ttf")
        if not os.path.exists(p):
            try:
                scarica(url, p)
            except Exception as e:
                log("font non scaricato:", nome, e)


# ------------------------------------------------------------------ ffmpeg
def ff(args, label=""):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + args
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg {label}: {p.stderr.strip()[-900:]}")


def probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                        "-of", "json", path], capture_output=True, text=True)
    try:
        j = json.loads(p.stdout)
    except ValueError:
        return {"dur": 0, "audio": False, "w": 0, "h": 0}
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"dur": float(j.get("format", {}).get("duration", 0) or 0),
            "audio": any(s.get("codec_type") == "audio" for s in j.get("streams", [])),
            "w": v.get("width", 0), "h": v.get("height", 0)}


# ------------------------------------------------------------------ colorista: LUT generate
LOOKS = {
    #             lift(ombre)      gain(luci)       sat   contrasto  tinta ombre        tinta luci
    "mediterraneo": dict(lift=0.02, gain=1.02, sat=1.12, con=1.08, sh=(-0.02, 0.01, 0.04), hi=(0.05, 0.02, -0.04)),
    "tramonto":     dict(lift=0.03, gain=1.00, sat=1.15, con=1.10, sh=(0.02, -0.01, 0.03), hi=(0.09, 0.03, -0.07)),
    "pellicola":    dict(lift=0.06, gain=0.97, sat=0.88, con=0.96, sh=(0.01, 0.02, 0.03), hi=(0.04, 0.02, -0.02)),
    "fresco":       dict(lift=0.01, gain=1.03, sat=1.06, con=1.06, sh=(-0.02, 0.01, 0.05), hi=(-0.01, 0.01, 0.03)),
    "naturale":     dict(lift=0.0, gain=1.0, sat=1.04, con=1.03, sh=(0, 0, 0), hi=(0, 0, 0)),
    "notte":        dict(lift=0.02, gain=1.0, sat=1.05, con=1.12, sh=(-0.03, 0.0, 0.06), hi=(0.08, 0.04, -0.03)),
}


def crea_lut(nome):
    """LUT 3D .cube: lift/gamma/gain, contrasto a S, saturazione, split-toning (ombre fredde/luci calde)."""
    L = LOOKS.get(nome, LOOKS["naturale"])
    path = os.path.join(TMP, f"look_{nome}.cube")
    n = 25
    righe = [f"LUT_3D_SIZE {n}"]
    for bi in range(n):
        for gi in range(n):
            for ri in range(n):
                r, g, b = ri / (n - 1), gi / (n - 1), bi / (n - 1)
                out = []
                luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
                for k, c in enumerate((r, g, b)):
                    c = L["lift"] + c * (L["gain"] - L["lift"])
                    c = 0.5 + (c - 0.5) * L["con"]  # contrasto
                    c += L["sh"][k] * (1 - luma) ** 2 + L["hi"][k] * luma ** 2  # split-toning
                    out.append(c)
                l2 = 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]
                out = [l2 + (c - l2) * L["sat"] for c in out]
                righe.append(" ".join(f"{min(1, max(0, c)):.5f}" for c in out))
    with open(path, "w") as f:
        f.write("\n".join(righe) + "\n")
    return path


# ------------------------------------------------------------------ audio: battiti
def battiti(musica_file, inizio=0.0):
    """Tempi dei battiti (secondi) con librosa; se non c'è, griglia a 110 bpm."""
    try:
        import librosa  # type: ignore
        y, sr = librosa.load(musica_file, sr=22050, mono=True, offset=inizio, duration=90)
        tempo, frames = librosa.beat.beat_track(y=y, sr=sr)
        bt = librosa.frames_to_time(frames, sr=sr).tolist()
        bpm = float(tempo if not hasattr(tempo, "__len__") else tempo[0])
        if len(bt) > 8:
            log(f"battiti trovati: {len(bt)} ({bpm:.0f} bpm)")
            return bt, bpm
    except Exception as e:
        log("analisi battiti non disponibile:", str(e)[:120])
    bpm = 110.0
    return [i * 60 / bpm for i in range(200)], bpm


def durate_a_tempo(n, beats, bpm, stile, durate_volute):
    """Assegna a ogni clip una durata che termina su un battito."""
    beat = 60.0 / max(60, min(180, bpm))
    if stile == "ritmo":
        base = [2 if beat < 0.45 else 1] * n            # 1-2 battiti a taglio (~0.5-1.1 s)
        base = [b * (2 if i in (0, n - 1) else 1) for i, b in enumerate(base)]  # gancio e chiusura un po' più lunghi
        while sum(base) * beat < 9 and n:                 # almeno ~9 s di video
            base = [b + 1 for b in base]
    else:  # cinematico
        base = [max(2, round(2.6 / beat)) for _ in range(n)]
    out, t = [], 0.0
    for i in range(n):
        if durate_volute and durate_volute[i]:
            d = float(durate_volute[i])
        else:
            d = base[i] * beat
        # aggancio la fine al battito più vicino
        fine = t + d
        if beats:
            vicino = min(beats, key=lambda b: abs(b - fine))
            if abs(vicino - fine) < beat * 0.6 and vicino - t > 0.4:
                fine = vicino
        out.append(round(fine - t, 3))
        t = fine
    return out


# ------------------------------------------------------------------ preparazione dei pezzi
def filtro_movimento(eff, dur, i):
    """Motion designer: movimenti sul pezzo già in 1080x1920."""
    if eff == "punch":      # zoom secco sul battito, poi assestamento
        return f"scale=w='{W}*(1+0.14*max(0,1-t/0.18))':h=-2:eval=frame,crop={W}:{H}"
    if eff == "zoom_avanti":
        return f"scale=w='{W}*(1+0.08*t/{dur:.2f})':h=-2:eval=frame,crop={W}:{H}"
    if eff == "zoom_indietro":
        return f"scale=w='{W}*(1.08-0.08*t/{dur:.2f})':h=-2:eval=frame,crop={W}:{H}"
    if eff == "tremolio":
        return (f"scale={int(W*1.05)}:{int(H*1.05)},crop={W}:{H}:x='(iw-{W})/2+9*sin(t*31+{i})':y='(ih-{H})/2+9*cos(t*23+{i})'")
    return ""


def prepara_pezzo(i, c, lut, stile, fx):
    src, dur = c["file"], float(c["durata"])
    out = os.path.join(TMP, f"pezzo_{i:02d}.mp4")
    eff = c.get("effetto") or ("punch" if stile == "ritmo" and i > 0 else "zoom_avanti")
    look = f"normalize=blackpt=black:whitept=white:smoothing=24:independence=0.4:strength=0.35,lut3d='{lut}'"
    extra = []
    if fx.get("grana"):
        extra.append("noise=alls=7:allf=t")
    if fx.get("vignetta", True):
        extra.append("vignette=PI/5.5")
    if fx.get("bande_cinema"):
        bh = int(H * 0.06)
        extra.append(f"drawbox=x=0:y=0:w=iw:h={bh}:color=black:t=fill,drawbox=x=0:y=ih-{bh}:w=iw:h={bh}:color=black:t=fill")
    post = ",".join([look] + extra)
    mov = filtro_movimento(eff, dur, i)
    if c.get("tipo") == "foto":
        frames = int(round(dur * FPS))
        z = "min(1+0.0011*on,1.3)" if eff != "zoom_indietro" else "max(1.3-0.0011*on,1)"
        vf = (f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},setsar=1,{post}")
        ff(["-loop", "1", "-i", src, "-t", f"{dur:.3f}", "-vf", vf, "-an"] + X264 + [out], f"foto {i}")
        return out
    vin = float(c.get("in", 0))
    vel = float(c.get("velocita", 1.0)) or 1.0
    ramp = c.get("ramp")  # rallenty sul finale del pezzo (speed ramp)
    riempi = c.get("adatta", "riempi") != "sfocato"
    if riempi:
        geom = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    else:
        geom = None
    if ramp:
        a = dur * 0.55                     # parte a velocità piena
        b = dur - a                         # parte al rallenty (0.4x)
        s1, s2 = a * vel, b * 0.4
        chain = (f"[0:v]trim=start={vin:.3f}:duration={s1 + s2:.3f},setpts=PTS-STARTPTS,split=2[p1][p2];"
                 f"[p1]trim=duration={s1:.3f},setpts=(PTS-STARTPTS)/{vel}[q1];"
                 f"[p2]trim=start={s1:.3f},setpts=(PTS-STARTPTS)/0.4[q2];[q1][q2]concat=n=2:v=1:a=0,fps={FPS}")
    else:
        chain = f"[0:v]trim=start={vin:.3f}:duration={dur * vel:.3f},setpts=(PTS-STARTPTS)/{vel},fps={FPS}"
    if geom:
        chain += f",{geom}"
    else:
        chain += (f",split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:3,eq=brightness=-0.08[bg];"
                  f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    chain += (f",{mov}" if mov else "") + f",setsar=1,{post},trim=duration={dur:.3f}[v]"
    ff(["-i", src, "-filter_complex", chain, "-map", "[v]", "-an"] + X264 + [out], f"clip {i}")
    return out


def bagliore(dur, i):
    """Bagliore di luce (light leak) generato: macchia calda che attraversa il quadro."""
    out = os.path.join(TMP, f"leak_{i}.mp4")
    ff(["-f", "lavfi", "-i", f"gradients=s={W//4}x{H//4}:c0=0xff9a3c:c1=0xffd27a:c2=0x000000:c3=0x000000:nb_colors=4:speed=0.02:d={dur}:r={FPS}",
        "-vf", f"scale={W}:{H},gblur=sigma=60,format=yuv420p"] + X264 + [out], "bagliore")
    return out


def unisci(pezzi, durate, stile, fx):
    """Transizioni (xfade): taglio secco sul battito per 'ritmo', con qualche scia/zoom/flash; dissolvenze per 'cinematico'."""
    out = os.path.join(TMP, "video.mp4")
    if stile == "ritmo":
        menu = fx.get("transizioni") or ["taglio", "taglio", "hblur", "taglio", "zoomin", "taglio", "fadewhite"]
        td = 0.18
    else:
        menu = fx.get("transizioni") or ["fade", "fade", "fadeblack", "fade"]
        td = 0.6
    if len(pezzi) == 1:
        shutil.copy(pezzi[0], out)
        return out, durate[0]
    args, fc = [], []
    for p in pezzi:
        args += ["-i", p]
    durate = [probe(p)["dur"] or d for p, d in zip(pezzi, durate)]  # durate reali (arrotondate ai fotogrammi)
    prev, total = "[0:v]", durate[0]
    for k in range(1, len(pezzi)):
        tr = menu[(k - 1) % len(menu)]
        tdk = 2 / FPS if tr == "taglio" else td
        trn = "fade" if tr == "taglio" else tr
        fc.append(f"{prev}[{k}:v]xfade=transition={trn}:duration={tdk}:offset={total - tdk:.3f}[x{k}]")
        prev = f"[x{k}]"
        total = total + durate[k] - tdk
    if fx.get("bagliori"):
        leak = bagliore(total, 0)
        args += ["-i", leak]
        fc.append(f"[{len(pezzi)}:v]format=gbrp[lk];{prev}format=gbrp[bs];[bs][lk]blend=all_mode=screen:all_opacity=0.22" + (f":enable='lt(t,{total - durate[-1]:.3f})'" if fx.get("_finale") else "") + ",format=yuv420p[xl]")
        prev = "[xl]"
    ff(args + ["-filter_complex", ";".join(fc), "-map", prev] + X264 + [out], "transizioni")
    return out, total


# ------------------------------------------------------------------ testi (ASS: titoli animati e sottotitoli parola per parola)
def ts(s):
    s = max(0.0, s)
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def ass_testo(t):
    return str(t).replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def scrivi_ass(path, titolo, scritte, parole, stile_sub):
    acc = stile_sub.get("colore", "&H0037D4FF")  # parola evidenziata (giallo caldo) in formato ASS BGR
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Titolo,Great Vibes,150,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,0,6,5,60,60,0,1\n"
            "Style: TitoloBold,Montserrat,96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,2,0,1,0,6,5,60,60,0,1\n"
            "Style: Scritta,Montserrat,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,3,3,2,80,80,640,1\n"
            "Style: Sub,Montserrat,74,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,2,70,70,560,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev = []
    if titolo and titolo.get("testo"):
        st = "Titolo" if titolo.get("stile", "calligrafia") == "calligrafia" else "TitoloBold"
        a, b = float(titolo.get("da", 0.15)), float(titolo.get("a", 2.4))
        parole_t = ass_testo(titolo["testo"]).split()
        # entrata parola per parola con piccolo "pop"
        testo = ""
        for k, p in enumerate(parole_t):
            t0 = int(k * 120)
            testo += f"{{\\alpha&HFF&\\t({t0},{t0 + 180},\\alpha&H00&)\\fscx85\\fscy85\\t({t0},{t0 + 220},\\fscx100\\fscy100)}}{p} "
        ev.append(f"Dialogue: 1,{ts(a)},{ts(b)},{st},,0,0,0,,{{\\an5\\pos(540,780)\\fad(0,250)}}{testo.strip()}")
    for s in scritte or []:
        ev.append(f"Dialogue: 0,{ts(float(s['da']))},{ts(float(s['a']))},Scritta,,0,0,0,,{{\\fad(150,150)\\move(540,1300,540,1280,0,300)\\an5}}{ass_testo(s['testo'])}")
    # sottotitoli parola per parola: gruppi di 3 parole, la parola detta in quel momento si accende
    gruppo = int(stile_sub.get("parole_per_riga", 3))
    for g in range(0, len(parole), gruppo):
        blocco = parole[g:g + gruppo]
        for k, w in enumerate(blocco):
            a = float(w["da"])
            b = float(blocco[k + 1]["da"]) if k + 1 < len(blocco) else float(w["a"]) + 0.05
            pezzi = []
            for j, x in enumerate(blocco):
                tx = ass_testo(x["testo"]).upper()
                pezzi.append(f"{{\\c{acc}\\fscx112\\fscy112}}{tx}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}" if j == k else tx)
            ev.append(f"Dialogue: 2,{ts(a)},{ts(b)},Sub,,0,0,0,,{' '.join(pezzi)}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(ev) + "\n")


def trascrivi(audio_file):
    """Trascrittore: parole con i tempi (faster-whisper); lista vuota se non disponibile."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segs, _ = model.transcribe(audio_file, language="it", word_timestamps=True, vad_filter=True)
        parole = [{"testo": w.word.strip(), "da": float(w.start), "a": float(w.end)} for s in segs for w in (s.words or []) if w.word.strip()]
        log(f"trascritte {len(parole)} parole")
        return parole
    except Exception as e:
        log("trascrizione non disponibile:", str(e)[:150])
        return []


def parole_da_sottotitoli(subs):
    """Sottotitoli già scritti (da/a/testo per frase) -> parole con tempi distribuiti."""
    out = []
    for s in subs or []:
        ws = str(s["testo"]).split()
        a, b = float(s["da"]), float(s["a"])
        for k, w in enumerate(ws):
            out.append({"testo": w, "da": a + (b - a) * k / len(ws), "a": a + (b - a) * (k + 1) / len(ws)})
    return out


# ------------------------------------------------------------------ finale e mix
def cartello_finale(cfg, lut):
    dur = float(cfg.get("durata", 2.4))
    out = os.path.join(TMP, "finale.mp4")
    col = cfg.get("colore", "0x1D2228")
    ff(["-f", "lavfi", "-i", f"color=c={col}:s={W}x{H}:r={FPS}:d={dur}", "-vf", "format=yuv420p", "-t", str(dur)] + X264 + [out], "finale")
    return out, dur


def whoosh(path, dur=0.35):
    ff(["-f", "lavfi", "-i", f"anoisesrc=d={dur}:c=pink:a=0.6", "-af",
        f"highpass=f=600,lowpass=f=5000,afade=t=in:d={dur*0.6:.2f},afade=t=out:st={dur*0.6:.2f}:d={dur*0.4:.2f},volume=0.5", path], "whoosh")
    return path


def mix_finale(video, dur, musica, voce, tagli, sfx, ass, out):
    """Audio: musica a norma social (-14 LUFS), si abbassa sotto la voce; whoosh sulle transizioni; testi impressi."""
    args = ["-i", video]
    filt, idx, mix = [], 1, []
    if musica:
        args += ["-i", musica["file"]]
        filt.append(f"[{idx}:a]atrim=start={float(musica.get('inizio', 0)):.2f}:duration={dur:.3f},asetpts=PTS-STARTPTS,"
                    f"volume={float(musica.get('volume', 0.9))},afade=t=in:d=0.3,afade=t=out:st={max(0, dur - 1.5):.2f}:d=1.5[mus]")
        idx += 1
    if voce:
        args += ["-i", voce]
        filt.append(f"[{idx}:a]apad,atrim=duration={dur:.3f},volume=1.6,asplit=2[voce][vk]")
        idx += 1
        if musica:
            filt.append("[mus][vk]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[musd]")
            mix += ["[musd]", "[voce]"]
        else:
            mix += ["[voce]"]
    elif musica:
        mix += ["[mus]"]
    if sfx and tagli:
        w = whoosh(os.path.join(TMP, "whoosh.wav"))
        for k, t in enumerate(tagli[:12]):
            args += ["-i", w]
            ms = max(0, int((t - 0.2) * 1000))
            filt.append(f"[{idx}:a]adelay={ms}|{ms}[w{k}]")
            mix.append(f"[w{k}]")
            idx += 1
    if not mix:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        filt.append(f"[{idx}:a]anull[aout]")
    else:
        filt.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    filt.append(f"[0:v]ass={ass}:fontsdir={FONTDIR}[vout]")
    ff(args + ["-filter_complex", ";".join(filt), "-map", "[vout]", "-map", "[aout]", "-t", f"{dur:.3f}",
               "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-maxrate", "6M", "-bufsize", "12M", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", out], "mix finale")


# ------------------------------------------------------------------ regia del montaggio
def monta(piano):
    prepara_font()
    stile = piano.get("stile", "ritmo")
    fx = dict({"vignetta": True}, **(piano.get("effetti") or {}))
    if stile == "cinematico":
        fx.setdefault("grana", True); fx.setdefault("bagliori", True); fx.setdefault("bande_cinema", False)
    lut = crea_lut(piano.get("colore", "mediterraneo"))
    clip = [dict(c) for c in piano["clip"]]
    for i, c in enumerate(clip):
        if c.get("tipo") == "avatar":  # arriva dalla voce (video HeyGen), più sotto
            continue
        ext = ".jpg" if c.get("tipo") == "foto" else ".mp4"
        c["file"] = c.get("file") or scarica(c["src"], os.path.join(TMP, f"src_{i:02d}{ext}"))
        c["_info"] = probe(c["file"]) if c.get("tipo") != "foto" else {"dur": 99}
    # musica e battiti
    mus = piano.get("musica")
    beats, bpm = None, 110.0
    if mus and (mus.get("src") or mus.get("file")):
        mus = dict(mus)
        try:
            mus["file"] = mus.get("file") or scarica(mus["src"], os.path.join(TMP, "musica.mp3"))
            beats, bpm = battiti(mus["file"], float(mus.get("inizio", 0)))
        except Exception as e:  # senza musica il reel si monta lo stesso (traccia ambiente)
            log("musica non scaricata, continuo senza:", str(e)[:120])
            mus = None
    else:
        mus = None
    # voce (avatar HeyGen o voce di Johnny): il video del parlato guida la durata
    voce_file, parole = None, []
    voce = piano.get("voce")
    if voce and (voce.get("src") or voce.get("file")):
        vf = voce.get("file") or scarica(voce["src"], os.path.join(TMP, "voce.mp4"))
        voce_file = os.path.join(TMP, "voce.wav")
        ff(["-i", vf, "-vn", "-ac", "1", "-ar", "16000", voce_file], "estrai voce")
        parole = trascrivi(voce_file) if piano.get("sottotitoli", "auto") == "auto" else parole_da_sottotitoli(piano.get("sottotitoli"))
        if voce.get("mostra", True):  # l'avatar è anche immagine: entra come primo pezzo e nei pezzi "avatar"
            for c in clip:
                if c.get("tipo") == "avatar":
                    c.update({"file": vf, "tipo": "video", "_info": probe(vf), "avatar": True, "effetto": c.get("effetto", "zoom_avanti")})
    elif isinstance(piano.get("sottotitoli"), list):
        parole = parole_da_sottotitoli(piano["sottotitoli"])
    clip = [c for c in clip if c.get("tipo") != "avatar"]  # pezzi avatar senza voce: tolti
    # durate: parlato -> pezzi coprono la voce; altrimenti a tempo di musica
    volute = [c.get("durata") for c in clip]
    if stile == "parlato" and voce_file:
        # il parlato guida il tempo: tagli secchi, l'avatar riprende sempre dal punto giusto (labbra a tempo)
        tot = probe(voce_file)["dur"] or 20
        n = len(clip)
        passo = tot / n
        sovr = 2 / FPS
        durate, t0 = [], 0.0
        for i, c in enumerate(clip):
            d = passo + (sovr if i < n - 1 else 0)
            if c.get("avatar"):
                c["in"] = t0
            durate.append(round(d, 3))
            t0 += passo
        fx["transizioni"] = ["taglio"]
    else:
        durate = durate_a_tempo(len(clip), beats, bpm, stile, volute)
    # non chiedere a una clip più di quanto dura
    for i, c in enumerate(clip):
        if c.get("tipo") != "foto":
            disp = max(0.5, c["_info"]["dur"] - float(c.get("in", 0)) - 0.05)
            fattore = (0.55 + 0.45 * 0.4) if c.get("ramp") else float(c.get("velocita", 1) or 1)
            if durate[i] * fattore > disp:
                durate[i] = round(disp / fattore, 3)
        c["durata"] = durate[i]
    # rallenty sul "pezzo eroe" (il più spettacolare, indicato dal regista)
    if stile == "ritmo" and not any(c.get("ramp") for c in clip):
        for c in clip:
            if c.get("ruolo") == "eroe" and c.get("tipo") != "foto":
                c["ramp"] = True
                break
    pezzi = []
    for i, c in enumerate(clip):
        log(f"pezzo {i + 1}/{len(clip)}: {c.get('tipo', 'video')} {c['durata']:.2f}s {c.get('effetto', '')}{' rallenty' if c.get('ramp') else ''}")
        pezzi.append(prepara_pezzo(i, c, lut, stile, fx))
    fin = piano.get("finale")
    if fin:
        p, d = cartello_finale(fin, lut)
        pezzi.append(p); durate.append(d)
        fx["_finale"] = True  # niente bagliori sul cartello finale
    video, tot = unisci(pezzi, durate, stile, fx)
    # testi: titolo d'apertura, scritte, cartello finale, sottotitoli
    scritte = list(piano.get("scritte") or [])
    if fin:
        inizio_fin = tot - durate[-1]
        # le scritte non invadono il cartello finale
        scritte = [dict(x, a=min(float(x["a"]), inizio_fin - 0.05)) for x in scritte if float(x["da"]) < inizio_fin - 0.6]
        scritte.append({"da": inizio_fin + 0.2, "a": tot, "testo": fin.get("sottotitolo", "avilletta.it · link in bio")})
    titolo = piano.get("titolo")
    if fin and fin.get("testo"):
        ass_fin = {"testo": fin["testo"], "stile": "calligrafia", "da": tot - durate[-1] + 0.1, "a": tot}
    else:
        ass_fin = None
    ass = os.path.join(TMP, "testi.ass")
    scrivi_ass(ass, titolo, scritte, parole, piano.get("stile_sottotitoli") or {})
    if ass_fin:  # secondo titolo (cartello finale) aggiunto in coda
        tmp = os.path.join(TMP, "fin.ass")
        scrivi_ass(tmp, ass_fin, [], [], {})
        with open(tmp, encoding="utf-8") as f:
            righe = [r for r in f.read().splitlines() if r.startswith("Dialogue:")]
        with open(ass, "a", encoding="utf-8") as f:
            f.write("\n".join(r.replace("\\pos(540,780)", "\\pos(540,900)") for r in righe) + "\n")
    tagli, t = [], 0.0
    for d in durate[:-1]:
        t += d
        tagli.append(t)
    out = os.path.join(TMP, "reel.mp4")
    mix_finale(video, tot, mus, voce_file, tagli, fx.get("whoosh", stile == "ritmo"), ass, out)
    log(f"reel pronto: {tot:.1f}s, {os.path.getsize(out) // 1024} KB, stile {stile}")
    return out, tot


def main():
    try:
        if os.environ.get("PIANO_FILE"):
            piano = json.load(open(os.environ["PIANO_FILE"]))
        else:
            piano = json.loads(regia(f"/api/montaggio/{JOB}/piano"))
        out, dur = monta(piano)
        if os.environ.get("OUT_FILE"):
            shutil.copy(out, os.environ["OUT_FILE"])
            return
        size = os.path.getsize(out)
        if size > 24 * 1024 * 1024:
            raise RuntimeError(f"video troppo pesante ({size // 1048576} MB): accorcia il reel")
        with open(out, "rb") as f:
            regia(f"/api/montaggio/{JOB}/risultato?durata={dur:.1f}", f.read(), ctype="video/mp4")
        log("consegnato alla Regia")
        if piano.get("anteprima"):  # solo per i montaggi di prova: un foglio di fotogrammi per il controllo
            try:
                d = os.path.join(os.environ.get("RUNNER_TEMP", TMP), "anteprima")
                os.makedirs(d, exist_ok=True)
                passo = max(dur / 12.0, 0.2)
                ff(["-i", out, "-vf", f"fps=1/{passo:.3f},scale=270:-2,tile=6x2:padding=4", "-frames:v", "1", "-q:v", "4",
                    os.path.join(d, f"montaggio-{JOB}.jpg")])
            except Exception as e:
                log("anteprima non creata:", e)
    except Exception as e:
        log("ERRORE:", e)
        if JOB and not os.environ.get("PIANO_FILE"):
            try:
                regia(f"/api/montaggio/{JOB}/errore", json.dumps({"errore": str(e)[:900]}).encode())
            except Exception as e2:
                log("non riesco a segnalare l'errore:", e2)
        sys.exit(1)


if __name__ == "__main__":
    main()
