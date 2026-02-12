# Release Checklist

## Before Release

- Pull latest `main` and confirm no unexpected local changes
- Run smoke checks:
  - `bash -n scripts/say.sh`
  - `python3 -m py_compile scripts/speak.py daemon/server.py`
- Verify path traversal protection for portraits still holds
- Confirm no secrets are committed:
  - `.env` is ignored
  - No API keys in tracked files
- Ensure generated artifacts are excluded:
  - `cache/` contains only `.gitkeep`
- Review docs for accuracy:
  - `README.md`
  - `SKILL.md`
  - `SECURITY.md`
- Confirm executable bits on scripts:
  - `scripts/say.sh`
  - `scripts/speak.py`

## Release

- Create a version tag: `git tag vX.Y.Z`
- Push code and tags: `git push origin main --tags`
- Create GitHub release notes from tag

## After Release

- Sanity test install from a fresh clone
- Validate daemon start and one end-to-end speak call
