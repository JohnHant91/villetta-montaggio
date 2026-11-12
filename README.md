# villetta-montaggio

Motore di montaggio video della Regia di A' Villetta B&B.

- Il workflow `.github/workflows/monta.yml` chiede alla Regia se c'è un reel da montare.
- La logica del montaggio (ffmpeg) arriva dalla Regia a ogni lavoro: qui non ci sono clip né chiavi.
- Il motore si identifica alla Regia con il token OIDC firmato da GitHub (`id-token: write`).
