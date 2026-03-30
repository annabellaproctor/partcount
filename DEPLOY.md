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

### Complete API Usage Tracking System
- **New database table**: `api_usage` tracks all external API calls
- **Rate limiting**: Enforced before paid API calls (Brave: 1000/mo, Tavily: 1000/mo)
- **Settings page monitor**: Live usage stats with color-coded progress bars
- **Metrics tracked**: Total calls, successful calls, avg response time, percentage used
- **Endpoint**: `/api/usage/stats?days=30` for programmatic access

### Multi-Source Image Search (5 sources)
1. **Wikimedia Commons** — free, unlimited, most reliable for electronics
2. **Mouser API** — requires `MOUSER_API_KEY`, unlimited
3. **DigiKey API** — requires `DIGIKEY_CLIENT_ID` + `DIGIKEY_TOKEN`, unlimited
4. **Brave Search Images** — requires `BRAVE_API_KEY`, 1000/month, 50/sec max
5. **Tavily AI Search** — requires `TAVILY_API_KEY`, 1000/month

### Mechanical Background Removal
- Replaced 176MB rembg model (30s cold start) with instant white threshold + numpy
- White pixels (R,G,B > 240) + near-white gradients (235-245) → transparent
- No AI models, no downloads, deterministic

### Interactive Crop/Rotate UI
1. Search shows Google Images-style grid with source badges
2. Click image → preview canvas with suggested crop coordinates
3. Adjust crop sliders (pre-filled with smart defaults: 5% padding)
4. Rotate: -90°, +90°, 180° buttons
5. Save → atomic file operation (temp → final)

## Environment Variables

Add to `.env` file (see `.env.example` for template):

```bash
# Required for basic operation
DATABASE_URL=postgresql+asyncpg://postgres:password@db:5432/labinventory
IMAGE_DIR=/app/images

# Image search APIs (all optional, Wikimedia works without any keys)
DIGIKEY_CLIENT_ID=your_client_id
DIGIKEY_CLIENT_SECRET=your_client_secret
DIGIKEY_TOKEN=your_access_token
MOUSER_API_KEY=your_mouser_key
BRAVE_API_KEY=your_brave_key        # 1000/month
TAVILY_API_KEY=your_tavily_key      # 1000/month

# AI parsing (optional)
GEMINI_API_KEY=your_gemini_key
```

## Database Migrations

Auto-runs on startup. New tables in this release:
- `api_usage` — tracks API calls for usage monitoring

Check migration status:
```bash
docker compose exec app python -c "from app.services.migrations import run_migrations; import asyncio; asyncio.run(run_migrations())"
```

## API Usage Monitoring

### Settings Page
- Navigate to `/settings`
- See "API Usage (30 days)" card on left
- Color coding:
  - 🟢 Green: <50% used
  - 🟡 Amber: 50-80% used
  - 🔴 Red: >80% used
- Click refresh button (↻) to update

### Programmatic Access
```bash
curl http://192.168.1.4:8000/api/usage/stats?days=30
```

Returns JSON with per-API stats:
```json
{
  "brave": {
    "name": "Brave Search",
    "total_calls": 45,
    "successful_calls": 43,
    "avg_response_ms": 234,
    "limit": 1000,
    "unit": "month",
    "percentage_used": 4.5
  }
}
```

## Rate Limit Behavior

When a rate limit is hit:
- API call is skipped
- Error logged: "API_NAME: monthly limit (LIMIT) exceeded"
- Other sources continue to work
- No user-facing error (graceful degradation)

Reset timing:
- Monthly limits reset on 1st of each month at 00:00 UTC
- Based on `api_usage.timestamp` column

## Logs

```bash
# Watch live logs
docker compose logs app -f

# Image search activity
docker compose logs app | grep "Image search"

# API usage tracking
docker compose logs app | grep "api_usage"

# Rate limit hits
docker compose logs app | grep "limit exceeded"

# Errors only
docker compose logs app | grep -i error
```

## Troubleshooting

### Image search returns 0 results
Check logs for which sources failed:
```bash
docker compose logs app | grep -A 5 "Image search"
```

Common issues:
- **Wikimedia**: Should always work (free, no auth)
- **Mouser API**: Verify `MOUSER_API_KEY` is valid
- **DigiKey**: Check token hasn't expired
- **Brave**: Verify key is correct, check usage hasn't hit 1000/month
- **Tavily**: Verify key is correct, check usage hasn't hit 1000/month

### API usage stats not showing
```bash
# Check if usage_stats router is loaded
docker compose logs app | grep "usage_stats"

# Test endpoint directly
curl http://192.168.1.4:8000/api/usage/stats
```

### Rate limit false positives
If you hit limits incorrectly, clear the usage table:
```sql
-- Connect to postgres
docker compose exec db psql -U postgres -d labinventory

-- Clear usage for specific API
DELETE FROM api_usage WHERE api_name = 'brave';

-- Or clear all usage
TRUNCATE api_usage;
```

### Background removal not working
- Mechanical method requires numpy
- Check: `docker compose exec app pip list | grep numpy`
- Should see: `numpy==1.26.4`
- Falls back to passthrough if numpy missing

### Crop preview blank
- Check browser console for JS errors
- Verify preview endpoint works:
```bash
curl -X POST http://192.168.1.4:8000/api/images/preview \
  -F "component_id=SOME_UUID" \
  -F "image_url=https://example.com/image.jpg"
```

## Performance Notes

- Wikimedia: ~200-400ms average response
- Mouser API: ~300-500ms average response
- DigiKey API: ~400-600ms average response
- Brave: ~150-300ms average response
- Tavily: ~500-800ms average response

All sources called in parallel where possible. Total search time typically <1 second.
