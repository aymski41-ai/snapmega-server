---
title: SnapMega Download API
sdk: docker
app_port: 7860
---

# SnapMega Download API

Backend for the SnapMega frontend. Supports public TikTok, Instagram, Facebook/Fb.watch and YouTube links when the platform allows access. Includes video download, MP3 extraction and clip trimming.

## Important

- This Space is a backend only; your website remains on Cloudflare Pages.
- Set the Space variable `FRONTEND_ORIGIN` to `https://snapmega.pages.dev` (or your exact website origin).
- Once the Space is running, open `/health` on its public `hf.space` URL. It should return JSON with `"ok": true`.
- Copy the Space's public API URL and replace `https://YOUR_BACKEND_URL` in `index.html` with that exact URL. Then publish the updated `index.html` to your Cloudflare Pages site.
- Do not expect downloads to work until the API has been deployed and the frontend URL updated.

## Limitations

Private, login-required, age-gated, DRM-protected or unavailable media may fail. Platform changes can also temporarily break support. Use only media you own or have permission to download and follow platform terms and local law.

This is a starter service, not a hardened public production API. Publicly exposing a downloader may attract heavy usage; free hosts can sleep, limit CPU/bandwidth, or suspend services.
