#!/usr/bin/env python3
"""Montatore della Regia di A' Villetta (gira sul motore GitHub o in locale per i test).

Legge il PIANO DI MONTAGGIO dalla Regia (o da un file con PIANO_FILE) e produce un reel 9:16:
- clip video: taglio in/out, velocità, adattamento 9:16 con fondo sfocato (mai ritaglio brutale)
- foto: movimento lento (zoom avanti/indietro, panoramica)
- scritte a schermo, sottotitoli, colore uniforme, grana, vignettatura
- transizioni (dissolvenza, zoom, scia, flash), cartello finale
- musica con dissolvenze e volume a norma social; traccia audio sempre presente
Solo libreria standard di Python + ffmpeg.
"""
import json, os, subprocess, sys, tempfile, urllib.request, shutil, math

REGIA = os.environ.get("REGIA", "https://avilletta-regia.gestionalelcl.workers.dev")
JOB = os.environ.get("JOB", "")
PASS = os.environ.get("PASS", "")
W, H, FPS = 1080, 1920, 30
TMP = tempfile.mkdtemp(prefix="monta_")

FONT_URLS = {
    "titolo": "https://raw.githubusercontent.com/google/fonts/main/ofl/greatvibes/GreatVibes-Regular.ttf",
    "testo": "https://raw.githubusercontent.com/google/fonts/main/ofl/josefinsans/JosefinSans%5Bwght%5D.ttf",
}
FALLBACK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

LOOKS = {  # colorista: look uniforme su tutte le clip
    "naturale": "eq=contrast=1.04:saturation=1.05",
    "mediterraneo": "eq=contrast=1.06:saturation=1.12:gamma=1.02,colorbalance=rs=0.03:bs=-0.03:rh=0.02:bh=-0.02",
    "tramonto": "eq=contrast=1.08:saturation=1.15,colorbalance=rs=0.08:gs=0.02:bs=-0.08:rm=0.04:bm=-0.04",
    "pellicola": "eq=contrast=0.95:saturation=0.85:gamma=1.03,colorbalance=rs=0.04:bs=-0.02,curves=preset=vintage",
    "fresco": "eq=contrast=1.05:saturation=1.08,colorbalance=bs=0.04:bh=0.03:rs=-0.02",
}


def log(*a):
    print("[montaggio]", *a, flush=True)


def regia(path, data=None, method=None, ctype="application/json"):
    req = urllib.request.Request(REGIA + path, data=data, method=method or ("POST" if data is not None else "GET"))
    req.add_header("x-montaggio", PASS)
    req.add_header("user-agent", "villetta-montaggio/1.0")
    if data is not None:
        req.add_header("content-type", ctype)
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def scarica(url, dest):
    if url.startswith("/"):
        url = REGIA + url
    req = urllib.request.Request(url, headers={"user-agent": "villetta-montaggio/1.0", "x-montaggio": PASS})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def ff(args, label=""):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + args
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg {label}: {p.stderr.strip()[-800:]}")


def durata(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def esc(t):
    # testo per drawtext: niente apici, due punti e percentuali "nudi"
    return (str(t).replace("\\", "\\\\").replace("'", "’").replace(":", "\\:").replace("%", "\\%").replace(",", "\\,"))


def font(kind):
    p = os.path.join(TMP, f"font_{kind}.ttf")
    if not os.path.exists(p):
        try:
            scarica(FONT_URLS[kind], p)
        except Exception as e:  # senza rete (test): font di sistema
            log("font non scaricato, uso quello di sistema:", e)
            return FALLBACK_FONT
    return p


def testo_filtri(scritta, dur, stile="testo", pos="basso"):
    """Scritta animata: entra con dissolvenza e leggero sollevamento, esce in dissolvenza."""
    if not scritta:
        return ""
    f = font("titolo" if stile == "titolo" else "testo")
    righe = spezza(scritta, 16 if stile == "titolo" else 22)
    size = adatta(150 if stile == "titolo" else 70, righe, f, stile)
    y0 = {"alto": 0.16, "centro": 0.42, "basso": 0.62}.get(pos, 0.62)
    out = []
    a_in, a_out = 0.35, 0.3
    alpha = f"if(lt(t,{a_in}),t/{a_in},if(gt(t,{dur - a_out:.2f}),max(0,({dur:.2f}-t)/{a_out}),1))"
    for i, riga in enumerate(righe):
        y = f"h*{y0}+{i}*{int(size * 1.15)}-{int(size * 0.25)}*max(0,1-t/{a_in})"
        out.append(
            f"drawtext=fontfile='{f}':text='{esc(riga)}':fontsize={size}:fontcolor=white:alpha='{alpha}'"
            f":x=(w-text_w)/2:y='{y}':shadowcolor=black@0.55:shadowx=3:shadowy=3"
        )
    return ",".join(out)


def adatta(base, righe, f, stile):
    """Riduce il corpo del testo finché la riga più lunga sta nel 86% della larghezza."""
    k = 0.42 if (stile == "titolo" and f != FALLBACK_FONT) else 0.62
    lunga = max((len(r) for r in righe), default=1)
    return max(40, min(base, int(W * 0.86 / (lunga * k))))


def spezza(t, n):
    parole, righe, cur = str(t).split(), [], ""
    for p in parole:
        if len(cur) + len(p) + 1 > n and cur:
            righe.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        righe.append(cur)
    return righe[:3]


def prepara_clip(i, c, look, effetti_globali):
    """Normalizza ogni pezzo a 1080x1920@30, durata esatta, con look, effetto e scritta."""
    src = c["file"]
    dur = float(c.get("durata") or 3)
    out = os.path.join(TMP, f"pezzo_{i:02d}.mp4")
    eff = c.get("effetto", "zoom_in")
    look_f = LOOKS.get(look, LOOKS["naturale"])
    extra = []
    if effetti_globali.get("grana"):
        extra.append("noise=alls=6:allf=t")
    if effetti_globali.get("vignetta", True):
        extra.append("vignette=PI/5")
    txt = testo_filtri(c.get("testo", ""), dur, c.get("stile_testo", "titolo" if i == 0 else "testo"), c.get("posizione_testo", "centro" if i == 0 else "basso"))
    post = ",".join([x for x in [look_f] + extra + ([txt] if txt else []) if x])

    if c.get("tipo") == "foto":
        frames = int(dur * FPS)
        z = {"zoom_in": f"zoom='min(1+0.0009*on,1.25)'", "zoom_out": f"zoom='max(1.25-0.0009*on,1)'",
             "pan_dx": "zoom='1.18'", "pan_sx": "zoom='1.18'"}.get(eff, "zoom='min(1+0.0009*on,1.25)'")
        x = {"pan_dx": f"x='(iw-iw/zoom)*on/{frames}'", "pan_sx": f"x='(iw-iw/zoom)*(1-on/{frames})'"}.get(eff, "x='iw/2-(iw/zoom/2)'")
        vf = (f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
              f"zoompan={z}:{x}:y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},setsar=1,{post}")
        ff(["-loop", "1", "-i", src, "-t", f"{dur:.3f}", "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", out], f"foto {i}")
    else:
        vin = float(c.get("in", 0))
        vel = float(c.get("velocita", 1.0)) or 1.0
        src_len = dur * vel
        # verticale: fondo sfocato + clip intera al centro (mai tagliare l'inquadratura originale)
        base = (f"[0:v]trim=start={vin:.3f}:duration={src_len:.3f},setpts=(PTS-STARTPTS)/{vel},fps={FPS},split=2[a][b];"
                f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:3,eq=brightness=-0.08[bg];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
        zoom = ""
        if eff == "punch_in":  # zoom improvviso a metà
            zoom = f",scale=w='{W}*if(gte(t,{dur/2:.2f}),1.15,1)':h=-2:eval=frame,crop={W}:{H}"
        elif eff in ("zoom_in", "zoom_lento"):
            zoom = f",scale=w='{W}*(1+0.06*t/{dur:.2f})':h=-2:eval=frame,crop={W}:{H}"
        fc = base + zoom + f",setsar=1,{post},trim=duration={dur:.3f}[v]"
        ff(["-i", src, "-filter_complex", fc, "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS), out], f"clip {i}")
    return out, dur


def finale(cfg, look):
    dur = float(cfg.get("durata", 2.5))
    out = os.path.join(TMP, "finale.mp4")
    f1, f2 = font("titolo"), font("testo")
    col = cfg.get("colore", "0x1D2228")
    r1, r2 = cfg.get("testo", "Prenota diretto"), cfg.get("sottotitolo", "avilletta.it · link in bio")
    t1, t2 = esc(r1), esc(r2)
    s1, s2 = adatta(140, [r1], f1, "titolo"), adatta(54, [r2], f2, "testo")
    alpha = f"if(lt(t,0.5),t/0.5,1)"
    vf = (f"drawtext=fontfile='{f1}':text='{t1}':fontsize={s1}:fontcolor=white:alpha='{alpha}':x=(w-text_w)/2:y=h*0.40,"
          f"drawtext=fontfile='{f2}':text='{t2}':fontsize={s2}:fontcolor=white@0.9:alpha='{alpha}':x=(w-text_w)/2:y=h*0.53")
    inputs = ["-f", "lavfi", "-i", f"color=c={col}:s={W}x{H}:r={FPS}:d={dur}"]
    if cfg.get("logo_file"):
        inputs += ["-i", cfg["logo_file"]]
        fc = f"[1:v]scale=360:-1[lg];[0:v][lg]overlay=(W-w)/2:H*0.22,{vf}[v]"
        ff(inputs + ["-filter_complex", fc, "-map", "[v]", "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", out], "finale")
    else:
        ff(inputs + ["-vf", vf, "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", out], "finale")
    return out, dur


def unisci(pezzi, trans):
    """Transizioni tra i pezzi con xfade (dissolvenza, zoom, scia, flash)."""
    tipo = {"dissolvenza": "fade", "zoom": "zoomin", "scia": "slideleft", "flash": "fadewhite", "nero": "fadeblack",
            "taglio": None}.get(trans.get("tipo", "dissolvenza"), "fade")
    td = float(trans.get("durata", 0.35))
    out = os.path.join(TMP, "video.mp4")
    if tipo is None or len(pezzi) == 1:
        lst = os.path.join(TMP, "lista.txt")
        with open(lst, "w") as f:
            for p, _ in pezzi:
                f.write(f"file '{p}'\n")
        ff(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out], "concat")
        return out, sum(d for _, d in pezzi)
    args, fc, prev, offset = [], [], "[0:v]", 0.0
    for p, _ in pezzi:
        args += ["-i", p]
    total = pezzi[0][1]
    for k in range(1, len(pezzi)):
        offset = total - td
        lab = f"[x{k}]"
        fc.append(f"{prev}[{k}:v]xfade=transition={tipo}:duration={td}:offset={offset:.3f}{lab}")
        prev = lab
        total = total + pezzi[k][1] - td
    ff(args + ["-filter_complex", ";".join(fc), "-map", prev, "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS), out], "transizioni")
    return out, total


def sottotitoli_ass(subs, path):
    f = "Josefin Sans"
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Sub,{f},68,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,4,2,2,80,80,300,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    def ts(s):
        s = max(0, s); h = int(s // 3600); m = int(s % 3600 // 60); sec = s % 60
        return f"{h}:{m:02d}:{sec:05.2f}"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(head)
        for s in subs:
            fh.write(f"Dialogue: 0,{ts(float(s['da']))},{ts(float(s['a']))},Sub,,0,0,0,,{{\\fad(80,80)}}{str(s['testo']).replace(chr(10), ' ')}\n")


def audio_finale(video, dur, musica, subs, out):
    """Audio: musica a norma social con dissolvenze (o traccia muta), sottotitoli impressi."""
    args = ["-i", video]
    vf = None
    if subs:
        ass = os.path.join(TMP, "sub.ass")
        sottotitoli_ass(subs, ass)
        fontsdir = TMP  # i font scaricati stanno qui
        vf = f"ass={ass}:fontsdir={fontsdir}"
    if musica and musica.get("file"):
        args += ["-i", musica["file"]]
        vol = float(musica.get("volume", 0.9))
        start = float(musica.get("inizio", 0))
        af = (f"[1:a]atrim=start={start}:duration={dur:.3f},asetpts=PTS-STARTPTS,volume={vol},"
              f"afade=t=in:d=0.4,afade=t=out:st={max(0, dur - 1.2):.2f}:d=1.2,loudnorm=I=-14:TP=-1.5:LRA=11[a]")
        args += ["-filter_complex", af + (f";[0:v]{vf}[v]" if vf else ""), "-map", "[v]" if vf else "0:v", "-map", "[a]"]
    else:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        if vf:
            args += ["-vf", vf]
        args += ["-map", "0:v", "-map", "1:a"]
    args += ["-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out]
    ff(args, "audio e sottotitoli")


def monta(piano):
    look = piano.get("colore", "mediterraneo")
    eff = piano.get("effetti", {})
    pezzi = []
    for i, c in enumerate(piano["clip"]):
        c = dict(c)
        ext = ".mp4" if c.get("tipo") != "foto" else ".jpg"
        c["file"] = c.get("file") or scarica(c["src"], os.path.join(TMP, f"src_{i:02d}{ext}"))
        if c.get("tipo") != "foto":
            disp = durata(c["file"]) - float(c.get("in", 0))
            need = float(c.get("durata", 3)) * float(c.get("velocita", 1) or 1)
            if disp and need > disp:  # clip più corta del previsto: accorcio il pezzo
                c["durata"] = max(0.8, disp / float(c.get("velocita", 1) or 1) - 0.05)
        log(f"pezzo {i + 1}/{len(piano['clip'])}: {c.get('tipo', 'video')} {c.get('durata')}s {c.get('effetto', '')} {c.get('testo', '')[:30]}")
        pezzi.append(prepara_clip(i, c, look, eff))
    fin = piano.get("finale")
    if fin:
        fin = dict(fin)
        if fin.get("logo"):
            try:
                fin["logo_file"] = scarica(fin["logo"], os.path.join(TMP, "logo.png"))
            except Exception as e:
                log("logo non scaricato:", e)
        pezzi.append(finale(fin, look))
    video, dur = unisci(pezzi, piano.get("transizione", {"tipo": "dissolvenza", "durata": 0.35}))
    mus = piano.get("musica")
    if mus and mus.get("src"):
        mus = dict(mus)
        mus["file"] = mus.get("file") or scarica(mus["src"], os.path.join(TMP, "musica.mp3"))
    out = os.path.join(TMP, "reel.mp4")
    audio_finale(video, dur, mus, piano.get("sottotitoli") or [], out)
    log(f"reel pronto: {dur:.1f}s, {os.path.getsize(out) // 1024} KB")
    return out, dur


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
