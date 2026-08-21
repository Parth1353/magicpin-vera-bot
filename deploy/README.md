# Deploying the bot

The judge needs one public base URL serving `/v1/*`. Whatever you use, two constraints
matter more than the platform:

1. **One instance, one worker.** Context lives in the process for the length of the test
   window. Two replicas means half the pushes land on a bot that never sees the other half.
2. **No sleeping.** The harness polls `/v1/healthz` every 60s and three consecutive
   failures disqualify the bot for that slot. Free tiers that idle-stop will fail this.

## Fastest: Render

```bash
render blueprint launch
```

Or in the UI: New → Web Service → point at this repo → Docker → health check `/v1/healthz`
→ instance type Starter (not Free) → 1 instance.

## Fly.io

```bash
fly launch --no-deploy --copy-config --config deploy/fly.toml
fly deploy
```

## Any Docker host

```bash
docker build -t vera-bot .
docker run -p 8080:8080 vera-bot
```

## Local, exposed through a tunnel

```bash
python run.py
```

```bash
ngrok http 8080
```

## Verifying before you submit

```bash
python tools/harness_sim.py --url https://your-host
```

That runs the full judge lifecycle — warmup, 12 ticks, mid-test context injection, the
operational contract, and the replay personas — against the deployed URL.

## Optional model polish

The composer is deterministic and needs no API key. To enable the editorial pass:

```bash
VERA_LLM_ENABLED=true VERA_LLM_PROVIDER=anthropic VERA_LLM_API_KEY=sk-... python run.py
```

An edit is only accepted if it introduces no new number and still passes the output guard;
otherwise the deterministic body ships unchanged.
