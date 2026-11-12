
// ------------------------------------------------------------------ Motore di montaggio (GitHub Actions)
const MONTA_REPO = 'johnhant91/villetta-montaggio';
const b64u = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(s.length / 4) * 4, '=')), (c) => c.charCodeAt(0));
async function oidcGitHub(auth) {
  const tok = String(auth || '').replace(/^Bearer\s+/i, '').trim();
  const parts = tok.split('.');
  if (parts.length !== 3) return null;
  let hdr, pl;
  try { hdr = JSON.parse(new TextDecoder().decode(b64u(parts[0]))); pl = JSON.parse(new TextDecoder().decode(b64u(parts[1]))); } catch { return null; }
  const aud = Array.isArray(pl.aud) ? pl.aud : [pl.aud];
  if (pl.iss !== 'https://token.actions.githubusercontent.com' || !aud.includes('avilletta-regia')) return null;
  if (String(pl.repository || '').toLowerCase() !== MONTA_REPO || !pl.exp || pl.exp * 1000 < Date.now()) return null;
  const jwks = await (await fetch('https://token.actions.githubusercontent.com/.well-known/jwks', { cf: { cacheTtl: 3600, cacheEverything: true } })).json();
  const jwk = (jwks.keys || []).find((k) => k.kid === hdr.kid);
  if (!jwk) return null;
  const key = await crypto.subtle.importKey('jwk', jwk, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
  const ok = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, b64u(parts[2]), new TextEncoder().encode(parts[0] + '.' + parts[1]));
  return ok ? pl : null;
}
const MEDIA_OK = /^(arch|archt|canva|ref|musica|clip|video)\//;
async function montaggioApi(req, env, url) {
  const p = url.pathname, method = req.method;
  // 1) il motore chiede un lavoro (identità GitHub verificata)
  if (p === '/api/montaggio/prossimo' && method === 'POST') {
    const who = await oidcGitHub(req.headers.get('authorization'));
    if (!who) return json({ error: 'motore non riconosciuto' }, 401);
    // un lavoro rimasto a metà per più di 45 minuti torna in coda (max 3 tentativi)
    await run(env, "UPDATE montaggi SET stato=CASE WHEN tentativi>=3 THEN 'errore' ELSE 'in_coda' END, errore=CASE WHEN tentativi>=3 THEN 'motore fermo per 3 volte' ELSE errore END WHERE stato='in_lavorazione' AND aggiornato < ?", new Date(Date.now() - 45 * 60000).toISOString());
    const pass = [...crypto.getRandomValues(new Uint8Array(24))].map((b) => b.toString(16).padStart(2, '0')).join('');
    const r = await all(env, "UPDATE montaggi SET stato='in_lavorazione', pass=?, tentativi=tentativi+1, aggiornato=? WHERE id IN (SELECT id FROM montaggi WHERE stato='in_coda' ORDER BY id LIMIT 1) RETURNING id, item_id", pass, now());
    if (!r.length) return new Response(null, { status: 204 });
    await log(env, 'montatore', `🎬 Il motore ha preso il montaggio #${r[0].id}`, { item_id: r[0].item_id || null });
    return json({ id: r[0].id, pass });
  }
  const m = p.match(/^\/api\/montaggio\/(\d+)\/(script\.py|piano|media|risultato|errore)$/);
  if (!m) return json({ error: 'non trovato' }, 404);
  const row = await one(env, 'SELECT * FROM montaggi WHERE id=?', Number(m[1]));
  const pass = req.headers.get('x-montaggio') || '';
  if (!row || !row.pass || row.stato !== 'in_lavorazione' || pass.length < 32 || pass !== row.pass) return json({ error: 'lasciapassare non valido' }, 403);
  const azione = m[2];
  if (azione === 'script.py') {
    const s = await env.MEDIA_KV.get('montaggio/lavoro.py');
    if (!s) return json({ error: 'script mancante' }, 500);
    return new Response(s, { headers: { 'content-type': 'text/x-python; charset=utf-8', 'cache-control': 'no-store' } });
  }
  if (azione === 'piano') {
    const piano = J(row.piano, {});
    // i media dell'archivio (chiavi KV) diventano indirizzi protetti dal lasciapassare
    const dove = (k) => `/api/montaggio/${row.id}/media?k=${encodeURIComponent(k)}`;
    for (const c of piano.clip || []) if (c.kv && !c.src) c.src = dove(c.kv);
    if (piano.musica?.kv && !piano.musica.src) piano.musica.src = dove(piano.musica.kv);
    if (piano.finale?.logo_kv && !piano.finale.logo) piano.finale.logo = dove(piano.finale.logo_kv);
    return json(piano);
  }
  if (azione === 'media' && method === 'GET') {
    const k = url.searchParams.get('k') || '';
    if (!MEDIA_OK.test(k)) return json({ error: 'media non consentito' }, 403);
    const o = await env.MEDIA_KV.getWithMetadata(k, { type: 'stream' });
    if (!o.value) return json({ error: 'media non trovato' }, 404);
    return new Response(o.value, { headers: { 'content-type': o.metadata?.type || 'application/octet-stream', 'cache-control': 'no-store' } });
  }
  if (azione === 'risultato' && method === 'POST') {
    const buf = await req.arrayBuffer();
    if (buf.byteLength < 10000) return json({ error: 'video vuoto' }, 400);
    if (buf.byteLength > 25 * 1024 * 1024) return json({ error: 'video oltre 25 MB' }, 413);
    const key = `video/montaggio-${row.id}-${Date.now()}.mp4`;
    await env.MEDIA_KV.put(key, buf, { metadata: { type: 'video/mp4' } });
    const dur = Number(url.searchParams.get('durata')) || null;
    await run(env, "UPDATE montaggi SET stato='pronto', risultato=?, durata=?, pass=NULL, aggiornato=? WHERE id=?", key, dur, now(), row.id);
    if (row.item_id) {
      await mergeContent(env, row.item_id, { montaggio: { stato: 'pronto', video: key, durata: dur, id: row.id, pronto_il: now() } }, { status: 'da_approvare' });
      const it = await one(env, 'SELECT tema, job_id FROM items WHERE id=?', row.item_id);
      await log(env, 'montatore', `🎞️ Reel "${it?.tema || ''}" montato (${dur ? dur.toFixed(1) + 's' : ''}): pronto per la tua approvazione`, { item_id: row.item_id, job_id: it?.job_id || null });
    } else await log(env, 'montatore', `🎞️ Montaggio di prova #${row.id} pronto (${dur ? dur.toFixed(1) + 's' : ''})`);
    return json({ ok: true, key });
  }
  if (azione === 'errore' && method === 'POST') {
    let e = ''; try { e = String((await req.json()).errore || ''); } catch {}
    const stop = row.tentativi >= 3;
    await run(env, 'UPDATE montaggi SET stato=?, errore=?, pass=NULL, aggiornato=? WHERE id=?', stop ? 'errore' : 'in_coda', e.slice(0, 900), now(), row.id);
    await log(env, 'montatore', `⚠️ Montaggio #${row.id} non riuscito${stop ? '' : ' (riprovo)'}: ${e.slice(0, 160)}`, { item_id: row.item_id || null, level: 'warn' });
    return json({ ok: true });
  }
  return json({ error: 'metodo non consentito' }, 405);
}
