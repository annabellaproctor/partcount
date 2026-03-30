# Deployment

Deploy to 192.168.1.4:

```bash
cd ~/labinventory
git pull
docker compose up -d --build
docker compose logs app -f --tail=50
```

Access at: http://192.168.1.4:8000

## What Changed (Latest)

### Image System Overhaul
- **Multi-source search**: DigiKey API, Mouser scraping, DuckDuckGo, Openverse, Wikimedia
- **Fast mechanical BG removal**: Replaced 176MB rembg model with white threshold + numpy (no AI, instant)
- **Interactive crop/rotate**: Preview canvas with sliders, smart auto-crop suggestions, 90° rotation
- **Google Images grid**: Responsive masonry layout with source badges
- **Atomic file operations**: Temp files in /tmp → prevents Content-Length errors

### Workflow
1. Click "Search Web" on component detail page
2. Search shows up to 24 results from 5 sources
3. Click image → shows full preview with suggested crop
4. Adjust crop sliders + rotate if needed
5. Toggle "Remove background" for transparent PNG
6. Click "Save Image" → processed and saved atomically

### Technical
- `/api/images/preview` — returns base64 + suggested crop box
- `/api/images/fetch` — processes with crop/rotate params
- `_mechanical_bg_remove()` — white threshold (240+ RGB) + near-white (235-245)
- `_smart_crop()` — finds content bbox + adds 5% padding
- All processing in /tmp with UUID filenames
- No more rembg cold start hang

## Environment Variables

Required for full image search:
```
DIGIKEY_TOKEN=your_token
DIGIKEY_CLIENT_ID=your_client_id
```

Optional (already set in .env):
```
IMAGE_DIR=/app/images
```

## Database

Auto-migrates on startup. No manual intervention needed.

## Logs

```bash
# App logs
docker compose logs app -f

# All services
docker compose logs -f

# Check errors only
docker compose logs app | grep -i error
```

## Troubleshooting

### Images not showing up
- Check `docker compose logs app` for fetch errors
- Verify `/app/images/components/` exists in container
- Check file permissions: `docker compose exec app ls -la /app/images/components/`

### Background removal not working
- Mechanical method requires numpy — check `pip list | grep numpy` in container
- Falls back to passthrough if numpy missing

### Search returns 0 results
- DigiKey: check token validity
- Mouser/DDG: scraping may break if HTML changes
- Openverse/Wikimedia: should always work as fallback

### Crop preview blank
- Browser console for JS errors
- Check preview endpoint: `curl -X POST http://192.168.1.4:8000/api/images/preview -F component_id=UUID -F image_url=URL`
