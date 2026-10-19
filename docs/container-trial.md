# 4scribe isolated container trial (owner-runnable)

Status: artifacts committed; **NOT BUILT / NOT TESTED by opencode** because the
Docker socket is not accessible to the `opencode` user (see blocker below).
No production cutover; the host unit on `127.0.0.1:8093` is unchanged.

## Artifacts

| Path | Purpose |
| --- | --- |
| `Dockerfile` | one app+Silero+aligner image; external Ollama; pinned `python:3.14.4-slim` digest and `torch==2.14.1+cpu` |
| `.dockerignore` | deny-all allowlist: only `requirements-container.txt`, `src/`, `tests/` enter the context |
| `requirements-container.txt` | direct + critical transitive pins from the accepted host inventory |
| `deployment/container-trial.sh` | build, in-image hermetic tests, isolated host-network trial on 18093, two-stream authenticated loopback WS smoke, selected measurements, cleanup |

The image bakes no aligner weights, no token and no host/tailnet identifier.
The 1.8 GB aligner cache is a read-only bind mount; the Silero JIT is part of
its pinned wheel. `qwen-asr==0.0.6` is installed with `--no-deps` (as in the
accepted host venv) so the optional gradio/Flask UI tree is not pulled in.

## Exact owner commands

```bash
# 1) build + test + trial (stops its own container on exit)
sudo -u i40min /opt/40zen/services/4scribe/deployment/container-trial.sh

# equivalent explicit form, if preferred:
cd /opt/40zen/services/4scribe
sudo -u i40min docker build -t 4scribe-trial:local .
sudo -u i40min docker run --rm --network=none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m --entrypoint python \
  4scribe-trial:local -m pytest tests -q -p no:cacheprovider
```

The trial binds only `127.0.0.1:18093` via `--network=host`; there is no port
mapping. Host Ollama at `127.0.0.1:11434` is used unchanged. The trial token is
generated 0600 under `/tmp/opencode/4scribe-container-trial/trial.env` and is
never printed or passed on argv.

Public fixtures are recovered by the existing pinned fetcher
(`/tmp/opencode/4scribe-vad-shadow/fetch_fixtures.py`, ~2 MB, CC BY 4.0); the
real RU and EN smoke uses the existing `deployment/smoke_wss.py`.

## Acceptance recorded by the script

- build succeeds; in-image hermetic core tests pass;
- health reports `verifier=silero-vad-6.2.3`;
- real RU and EN two-stream loopback WS: partials advance, finals precede the
  commit ACK, word times match the final text and are monotonic, no error;
- selected `docker stats` memory/CPU and effective `torch.get_num_threads()`.

## Known blocker

`opencode` is not in the host `docker` group (`docker:x:983:i40min`) and cannot
read `/var/run/docker.sock`; `docker` commands fail with "permission denied".
No credential or permission bypass was attempted. The owner/primary running as
`i40min` can execute the commands above.

## Limits and rollback

Initial run sets no memory/CPU quota (measure first). A conservative cap is set
only after the measured two-stream peak, preserving the host core count.
Rollback/cleanup is `docker stop 4scribe-trial-18093 && docker rm -f
4scribe-trial-18093` (the script traps this itself) plus `docker rmi
4scribe-trial:local` if the image is no longer needed. Cutover to 8093 is a
separate owner gate and is not authorized here.
