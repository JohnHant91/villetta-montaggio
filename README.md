# villetta-montaggio

Motore di montaggio video della Regia di A' Villetta B&B.

- Il workflow `.github/workflows/monta.yml` chiede alla Regia se c'è un reel da montare.
- Il montatore è `lavoro.py` (ffmpeg); clip, piano di montaggio e video finito passano dalla Regia: qui non ci sono clip né chiavi.
- Il motore si identifica alla Regia con il token OIDC firmato da GitHub (`id-token: write`).
