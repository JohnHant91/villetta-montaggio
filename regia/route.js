    // ---------------------------------------------------------------- motore di montaggio (GitHub, nov 2026)
    // Il motore su GitHub (JohnHant91/villetta-montaggio) si presenta con il token OIDC firmato da GitHub,
    // prende un lavoro, scarica script/piano/media con un lasciapassare usa-e-getta e rimanda il video.
    if (url.pathname.startsWith('/api/montaggio/') && env.DB) {
      try { return await montaggioApi(req, env, url); }
      catch (e) { return json({ error: String(e.message || e).slice(0, 300) }, 500); }
    }
