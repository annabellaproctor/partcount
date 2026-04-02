# API Setup Guide

This guide helps you configure external APIs for image search and component lookup.

## Quick Start

1. Copy `.env.example` to `.env` if you haven't already
2. Add API keys below as you obtain them
3. Restart the app: `cd ~/labinventory && docker compose up -d --build`
4. Check Settings page at http://192.168.1.4:8000/settings to verify usage tracking

## Priority Order

**Start with these (free tier available):**
1. Wikimedia Commons — **No key needed, works out of the box**
2. Brave Search — 1000 free queries/month
3. Tavily — 1000 free queries/month

**Optional paid/unlimited:**
4. DigiKey API — Unlimited, requires OAuth setup
5. Mouser API — Unlimited, straightforward signup

## 1. Wikimedia Commons

**Status**: ✅ Already working, no configuration needed

**What it provides**: Electronics component images, free, unlimited

**Rate limit**: None

## 2. Brave Search API

**Cost**: Free tier: 1000 queries/month, 50 requests/second

**Sign up**: https://brave.com/search/api/

**Steps**:
1. Create account
2. Navigate to API Keys section
3. Generate new API key
4. Add to `.env`:
```
BRAVE_API_KEY=BSA...your_key_here
```

**What it provides**: High-quality image search across the web

**Rate limit tracking**: Automatically tracked in Settings page

## 3. Tavily AI Search

**Cost**: Free tier: 1000 queries/month

**Sign up**: https://tavily.com

**Steps**:
1. Sign up at tavily.com
2. Go to API Keys dashboard
3. Copy your API key
4. Add to `.env`:
```
TAVILY_API_KEY=tvly-...your_key_here
```

**What it provides**: AI-powered search, good at finding product images

**Rate limit tracking**: Automatically tracked in Settings page

## 4. DigiKey API

**Cost**: Free, unlimited (requires business verification)

**Sign up**: https://developer.digikey.com/

**Steps**:
1. Create DigiKey account
2. Apply for API access (business verification required)
3. Create OAuth app:
   - Production API checkbox
   - Note your Client ID and Client Secret
4. Generate access token (OAuth flow or use their token tool)
5. Add to `.env`:
```
DIGIKEY_CLIENT_ID=your_client_id
DIGIKEY_CLIENT_SECRET=your_client_secret
DIGIKEY_TOKEN=your_access_token
```

**What it provides**: 
- Component images directly from DigiKey
- Part specifications, pricing, availability
- Used in both image search and component lookup

**Rate limit**: None (unlimited)

**Note**: Token may expire, need to refresh periodically

## 5. Mouser API

**Cost**: Free, unlimited

**Sign up**: https://www.mouser.com/api-hub/

**Steps**:
1. Create Mouser account
2. Navigate to API Hub
3. Request API key (instant approval usually)
4. Add to `.env`:
```
MOUSER_API_KEY=your_key_here
```

**What it provides**: 
- Component images from Mouser catalog
- Part specifications, pricing, availability
- Used in both image search and component lookup

**Rate limit**: None (unlimited)

## 6. Gemini AI (Optional)

**Used for**: AI parsing in "Add Component" popup (paste text → auto-fill fields)

**Cost**: Free tier: Generous limits

**Sign up**: https://makersuite.google.com/app/apikey

**Steps**:
1. Go to Google AI Studio
2. Create API key
3. Add to `.env`:
```
GEMINI_API_KEY=AIza...your_key_here
AI_PROVIDER=auto
AI_OPENAI_BASE_URL=
AI_OPENAI_API_KEY=
AI_OPENAI_MODEL=gpt-4o-mini
AI_SOURCE_DIR=/tmp/lab-inventory-tracker/ai-sources
```

If Gemini is unavailable, the assistant can use any OpenAI-compatible endpoint instead. Long pastes and uploaded PDFs/text files are stored as reference sources and then passed to the AI as compact context rather than raw prompt text.

**Rate limit**: Not tracked (high free tier limits)

### Runtime Provider Manager (in-app)

You can now edit AI providers live in **Settings → AI Providers Runtime** without restarting the app.

Supported features:
- Enable/disable providers mid-session
- Edit provider base URL, API key, default model, and fallback models
- Choose routing strategy: `priority`, `round_robin`, or `weighted_random`
- Configure model per task (`assistant`, `parse`, `enrich`, `order`, `merge`, `classify`, `rename`)
- Test provider routing directly from the Settings page

Behavior:
- Requests route to a selected provider using the configured strategy
- If that provider fails, the system falls back to remaining enabled providers in order
- Task-specific model mappings are used before provider default model

## Testing Your Setup

After adding keys, test each source:

```bash
# Restart app
cd ~/labinventory
docker compose up -d --build

# Watch logs
docker compose logs app -f

# In another terminal, trigger a search by visiting component detail page
# and clicking "Search Web"

# Check which sources responded
docker compose logs app | grep "Image search" | tail -5
```

Expected output:
```
Wikimedia: 8 images (342ms)
Mouser API: 3 images (456ms)
DigiKey: 5 images (521ms)
Brave: 12 images (234ms)
Tavily: 4 images (678ms)
Image search 'ESP32' returned 24 total results
```

## Monitoring Usage

Navigate to Settings page: http://192.168.1.4:8000/settings

The "API Usage (30 days)" card shows:
- Total calls made to each API
- Success rate
- Average response time
- Percentage of rate limit used (for Brave and Tavily)
- Color-coded progress bars (green → amber → red)

## Cost Management

**Free tier limits**:
- Brave: 1000 queries/month
- Tavily: 1000 queries/month

**Tips to stay within limits**:
- Image search typically uses 1 query per source per search
- Settings page tracks usage in real-time
- Rate limiter automatically stops calls when limit hit
- Wikimedia has no limits and usually provides good results

**If you hit limits**:
- Wikimedia continues to work (no limits)
- DigiKey continues to work (unlimited)
- Mouser continues to work (unlimited)
- Only Brave/Tavily will stop until next month

## Troubleshooting

### "No Results" on image search

1. Check logs:
```bash
docker compose logs app | grep -A 2 "Image search errors"
```

2. Common issues:
- **"401" error**: API key invalid or expired
- **"403" error**: Rate limit hit or key not authorized
- **"429" error**: Rate limit hit
- **"500" error**: API service down

3. Test individual APIs:
```bash
# Test Brave
curl -H "X-Subscription-Token: YOUR_KEY" \
  "https://api.search.brave.com/res/v1/images/search?q=ESP32&count=5"

# Test Tavily
curl -X POST https://api.tavily.com/search \
  -H "Content-Type: application/json" \
  -d '{"api_key":"YOUR_KEY","query":"ESP32","include_images":true}'
```

### Rate limit hit early

If you're hitting rate limits before expected:
1. Check Settings page for actual usage
2. Verify no other systems are using same API keys
3. Check if rate limit window is monthly or daily

### DigiKey token expired

DigiKey tokens expire after some time:
1. Go to developer.digikey.com
2. Regenerate access token
3. Update `DIGIKEY_TOKEN` in `.env`
4. Restart: `docker compose up -d`

## Support

For API-specific issues:
- **Brave**: support@brave.com
- **Tavily**: support@tavily.com  
- **DigiKey**: api.support@digikey.com
- **Mouser**: Contact via API Hub

For lab-inventory issues:
- GitHub: github.com/p-sum/lab-inventory-tracker
- Check logs: `docker compose logs app -f`
