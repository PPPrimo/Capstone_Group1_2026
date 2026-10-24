# mkcert (optional, dev-only)

This folder is **optional** and only exists to make local HTTPS development nicer.

- `mkcert` is **not** for production.
- For `primowang.com`, use a publicly trusted certificate (Let’s Encrypt or Cloudflare).

## One-time setup
Run in an elevated PowerShell if needed:

```powershell
mkcert -install
```

## Generate a cert for this project
`RunWeb.bat` can call mkcert through this helper when enabled:

- Edit `RunWeb.bat` and set `USE_MKCERT=1`
- Then run `RunWeb.bat`

The cert is written to `Resources/ssl/server.crt` and key to `Resources/ssl/server.key`.
