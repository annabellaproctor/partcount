User does not like the logo you chose.
# Lab Inventory Tracker - Workspace Exploration Report

## 1. Current Logo/Branding in Static Assets and Base Template

### Brand Identity
- **Logo Text**: `LAB::INV` (stylized as `LAB<span>::</span>INV`)
- **Brand Location**: Top-left of the topbar in [templates/base.html](templates/base.html#L320)
- **Color**: Green (`--green: #3ddc84`) with separator in gray (`--text3: #686868`)
- **Font**: JetBrains Mono, configured as monospace system font stack

### Static Assets Status
- **Static Directory Structure**: Expected at `/app/app/static` (Docker) or workspace-local path
- **CSS/JS Files**: Mentioned in structure but directory appears empty or not mounted in workspace view
- **External Assets**: Uses CDN for:
  - Bootstrap 5.3.3 (CSS)
  - Google Fonts (JetBrains Mono)
  - HTMX 1.9.12 (dynamic HTML updates)
- **No favicon specified** in HTML head

---

## 2. Favicon Setup

**Status**: ❌ **Not Configured**
- No `<link rel="icon">` or `<link rel="apple-touch-icon">` in [templates/base.html](templates/base.html#L1-L11)
- No favicon file found in workspace
- **Recommendation**: Add favicon setup for branding

---

## 3. Mobile/Responsive Design Setup

### Viewport Configuration
```html
<meta name="viewport" content="width=device-width, initial-scale=1">
```
✅ **Standard viewport meta tag present** at [templates/base.html](templates/base.html#L3)

### Responsive Features
- **Bootstrap 5.3.3**: Provides grid system and responsive utilities
- **Flexbox Layout**: Heavy use of flexbox for flexible layouts
  - Topbar: `display: flex; align-items: center; gap: 0`
  - Navigation links: responsive wrapping with `gap: 12px`
- **Media Query Approach**: Not explicitly defined in visible CSS; Bootstrap handles responsive breakpoints

### Current Responsive Elements
- Top navigation bar: sticky, 42px height, responsive icon layout
- Command palette: centered modal with `max-width: 820px` for desktop, `padding: 36px 18px` for breathing room
- Tab-based content system with flex wrapping
- Cards and grids adapt to viewport

---

## 4. Current CSS Styling Approach

### Color Scheme (CSS Variables)
Located in [templates/base.html](templates/base.html#L12-L24):

**Dark Theme - "Hacker Aesthetic"**
```css
--bg:       #0a0a0a;    /* Pure black background */
--bg2:      #111111;    /* Primary container */
--bg3:      #181818;    /* Secondary container */
--bg4:      #1e1e1e;    /* Tertiary/hover state */
--border:   #242424;    /* Primary borders */
--border2:  #2e2e2e;    /* Secondary borders */
--green:    #3ddc84;    /* Primary accent (bright) */
--green2:   #2ab56d;    /* Secondary accent (darker) */
--green-bg: rgba(61,220,132,0.06);  /* Subtle background overlay */
--amber:    #f0a500;    /* Warning/paused state */
--red:      #e05555;    /* Error/danger state */
--blue:     #4a9eff;    /* Info/complete state */
--text:     #e8e8e8;    /* Primary text */
--text2:    #a0a0a0;    /* Secondary text */
--text3:    #686868;    /* Tertiary/muted text */
```

### Spacing System
- **Base padding**: `5px`, `7px`, `12px`, `14px`, `16px`, `18px`, `20px`
- **Border radius**: Unified `--radius: 3px` for all elements
- **Gap/spacing**: Consistent `gap: 8px`, `gap: 12px`, `gap: 16px`
- **Typography**: Monospace font throughout with size scaling

### Design Components
- **Cards**: Dark background with subtle borders
- **Tables**: Custom styled with zebra striping and hover effects
- **Forms**: Dark inputs with green focus states
- **Buttons**: Bordered style (no solid fill), green accent on hover
- **Badges**: Subtle background with green text
- **Status indicators**: Color-coded (green=active, amber=paused, blue=complete, red=error)

### Interactive States
- **Scrollbar**: Custom webkit styles (5px width, dark theme)
- **Focus states**: 2px solid green outline with shadow
- **Selection**: Semi-transparent green background
- **Hover transitions**: 0.15s color and border transitions

---

## 5. Base Template Structure (Header, Footer, Navigation)

### Topbar (Header) [Line 309-345]
```
LEFT SIDE:
  ├─ Brand Logo: "LAB::INV"
  └─ Navigation Links (uppercase, 11px)
     ├─ Dashboard
     ├─ Components
     ├─ Registry
     ├─ Kits
     ├─ Orders
     ├─ Boxes
     ├─ Projects
     ├─ Labels
     ├─ Scan
     └─ Settings

RIGHT SIDE:
  ├─ Command Palette Button (⌘K)
  ├─ Add Component Button (+ ADD)
  ├─ WebSocket Status Dot (live indicator)
  └─ Profile Badge (initials)
```

### Main Content Area
- **Class**: `.main`
- **Structure**: `padding: 20px 20px 140px` (top/sides, extra bottom for event log)
- **Flex**: Takes remaining vertical space with `flex: 1`
- **Content Block**: `{% block content %}{% endblock %}`

### Event Log (Footer) [Line 273-284]
```
FIXED AT BOTTOM (z-index: 200)
├─ Label: "EVENT LOG" (uppercase, 9px)
├─ Scrollable Container (height: 58px, reverse scroll)
└─ Log Lines (live feed)
    ├─ Scan events (green)
    ├─ Stock changes (amber)
    ├─ Errors (red)
    └─ Info (blue)
```

### Modals Overlaid
1. **Command Palette**: Quick search + navigation (Ctrl/Cmd+K)
2. **Shortcuts Modal**: Keyboard help (?)
3. **Add Popup**: Component creation form

### Footer Status
❌ **No traditional footer** - uses fixed event log bar instead

---

## 6. AI Usage Tracking and Display (Gemini, etc.)

### AI Service Implementation
Location: [app/routers/ai_parse.py](app/routers/ai_parse.py#L1-L50)

**Gemini Integration**:
```python
- SDK: google-genai (official SDK)
- Client: genai.Client(api_key=GEMINI_API_KEY)
- Models Supported:
  ├─ gemini-3.1-flash-lite-preview (500 RPD)
  ├─ gemini-2.5-flash-lite (20 RPD)
  └─ gemini-3-flash (20 RPD)
- Rename Model: gemini-3-flash (fallback: environment var)
```

**Features**:
- Dynamic model selection based on quota availability
- 30-minute model check interval
- ThreadPoolExecutor with configurable max workers (default: 2)
- Asyncio semaphore for request rate limiting
- Failed request logging (max 50 entries)
- Timeout: Configurable (default: 10 seconds)

### Usage Stats Tracking
Location: [app/routers/usage_stats.py](app/routers/usage_stats.py)

**Endpoint**: `GET /api/usage/stats?days=30`

**Tracked APIs** (from [app/services/api_usage.py](app/services/api_usage.py)):
- DigiKey
- Mouser
- Gemini
- Brave (image search)
- Tavily (AI search)

**Metrics Returned**:
```json
{
  "api_name": {
    "name": "Display Name",
    "total_calls": 0,
    "successful_calls": 0,
    "avg_response_ms": null,
    "limit": 1000,
    "unit": "per month",
    "percentage_used": 0
  }
}
```

**Rate Limits Defined**:
```python
RATE_LIMITS = {
    "digikey": {"name": "DigiKey", "limit": 1000, "unit": "per month"},
    "mouser": {"name": "Mouser", "limit": 500, "unit": "per month"},
    "gemini": {"name": "Gemini", "limit": 1500, "unit": "per day"},
    "brave": {"name": "Brave", "limit": 100, "unit": "per month"},
    "tavily": {"name": "Tavily", "limit": 250, "unit": "per month"},
}
```

### API Sources Endpoint
Location: [app/main.py](app/main.py#L69-L75)

`GET /api/sources` returns:
```json
{
  "digikey": bool,      /* Environment: DIGIKEY_CLIENT_ID */
  "mouser": bool,       /* Environment: MOUSER_API_KEY */
  "gemini": bool,       /* Environment: GEMINI_API_KEY */
  "trustedparts": false /* Pending implementation */
}
```

---

## 7. Component Image Handling and Removal Logic

### Image Storage
- **Directory**: Environment variable `IMAGE_DIR` (default: `/app/images`)
- **Subdirectory**: `{IMAGE_DIR}/components/`
- **Naming**: Components stored as `{barcode_id}.png`
- **Format**: PNG with RGBA support

### Image Processing Pipeline
Location: [app/routers/images.py](app/routers/images.py#L246-L330)

**Main Processing Function**: `_process(src, dest, remove_bg=False, crop=None, rotate=0)`

**Features**:
1. **Background Removal**: `_mechanical_bg_remove()` (non-AI, deterministic)
   - White threshold: RGB all > 240 → transparent
   - Near-white detection: 235-245 range
   - Uses numpy + PIL for processing

2. **Smart Crop**: `_smart_crop()`
   - Finds content bounding box
   - Adds 5% padding on each side
   - Returns (left, top, right, bottom) coordinates

3. **Rotation**: 0, 90, 180, 270 degrees support
4. **Output**: Always PNG with RGBA

### Image Endpoints

**1. Search Images**
- `GET /api/images/search?q=<query>&limit=20`
- **Sources** (in priority order):
  1. Wikimedia Commons (free, always available)
  2. Mouser API
  3. DigiKey API
  4. Brave Search (image-specific)
  5. Tavily (AI-powered search)
- **Deduplication**: MD5 hash of URL
- **Rate Limiting**: Checked before each API call

**2. Preview Image** `POST /api/images/preview`
- Download without saving
- Returns:
  - Base64 preview
  - Image dimensions
  - Suggested crop coordinates

**3. Fetch & Save** `POST /api/images/fetch`
- Download from URL
- Process (optional BG removal, crop, rotate)
- Save to disk atomically
- Update `Component.image_path` in DB
- Used by external image search UI

**4. Upload Image** `POST /api/images/upload/{component_id}`
- Accept file upload
- Same processing pipeline
- Query parameters:
  - `remove_bg`: Boolean
  - `rotate`: 0|90|180|270
  - `crop_left`, `crop_top`, `crop_right`, `crop_bottom`: Pixel coords

**5. Proxy Image** `POST /api/images/proxy`
- Fetch external image
- Return as base64 data URL
- Avoids CORS issues in crop editor

### Image Deletion Logic
❌ **NOT FOUND** - No explicit image delete endpoint visible
- Images are overwritten when new image is uploaded for same component
- Component deletion likely orphans images (no cleanup logic found)
- **Recommendation**: Implement image garbage collection

---

## 8. Current Shortcut Implementation / Keyboard Handling

Location: [templates/base.html](templates/base.html#L495-L575)

### Global Shortcuts
```
Ctrl/Cmd+K       → Open command palette (search/navigation)
?                → Open keyboard shortcuts help
A                → Open add component popup
Esc              → Close overlays (command palette, shortcuts, add popup)
Enter (w/ data)  → HID scanner trigger (buffer >= 2 chars)
ArrowUp/Down     → Navigate command palette results

Scanner Buffer:
  - Accumulates printable characters
  - Triggers on Enter if buffer length > 2
  - 500ms timeout to clear buffer
```

### Command Palette
- **Trigger**: Ctrl/Cmd+K or ⌘K button
- **Index**: Lazy-loaded from `/api/search-index?limit=500`
- **Search**: Fuzzy match on label, route, tokens, kind
- **Navigation**: Arrow keys to select, Enter to open
- **Max Results**: 80 matches shown, 40 without query

### Add Component Popup
- **Trigger**: 'A' key or "+ ADD" button
- **Shift+Click**: Opens in new tab instead of modal
- **Focus**: Auto-focuses lookup input field
- **Forms**: Dynamic type fields based on selected component type

### Shortcuts Modal
Location: [templates/base.html](templates/base.html#L357-L366)
```
Ctrl/Cmd+K       → global search
?                → open this menu
A                → open add component popup
Esc              → close overlays

Note: "Page-specific shortcuts are shown on those pages."
```

### Page-Specific Shortcuts

**Orders Page** ([templates/orders.html](templates/orders.html#L524-L544)):
```
/                → Focus orders search
Alt+N            → Add component row
Alt+K            → Add kit row
Alt+S            → Create order from draft
?                → Toggle help panel
Esc              → Close help/detail modals
```

### Keyboard Accessibility Features
- **Keyboard Navigation**: Non-semantic clickable elements made keyboard accessible
  - Added `tabindex="0"` to onclick elements
  - Added `role="button"` for screen readers
- **Focus Indicators**: 2px green outline with shadow
- **Button Accessibility**: Enter or Space keys trigger buttons

### HTML Scanner Support
- Global keydown listener accumulates printable characters
- Simulates barcode scanner data entry
- Payload format: `{barcode_id}` or `{barcode_id}:quantity`
- Endpoint: `POST /api/components/{barcode_id}/scan`

---

## 9. PWA/Manifest Files

**Status**: ❌ **Not Configured**

### Missing Files
- No `manifest.json` found
- No service worker file (`sw.js` or similar)
- No PWA icons directory

### Current Setup
- Standard viewport meta tag present
- No PWA-specific meta tags:
  - ❌ `<meta name="theme-color">`
  - ❌ `<meta name="apple-mobile-web-app-capable">`
  - ❌ `<link rel="manifest">`
  - ❌ `<link rel="icon" type="image/png">`

**Recommendation**: Implement PWA support if offline capability desired

---

## Summary Statistics

| Aspect | Status | Location |
|--------|--------|----------|
| Branding | ✅ Complete | [templates/base.html](templates/base.html#L320) |
| Favicon | ❌ Missing | N/A |
| Responsive Design | ✅ Partial (Flexbox + Bootstrap) | [templates/base.html](templates/base.html#L1-L300) |
| CSS Framework | ✅ Custom + Bootstrap 5.3.3 | Built-in styles + CDN |
| Dark Theme | ✅ Complete | CSS variables, [templates/base.html](templates/base.html#L12-L24) |
| Navigation | ✅ Complete | Topbar + Command palette |
| Footer | ⚠️ Event log only | [templates/base.html](templates/base.html#L273-L284) |
| AI Integration (Gemini) | ✅ Complete | [app/routers/ai_parse.py](app/routers/ai_parse.py) |
| Usage Tracking | ✅ Complete | [app/routers/usage_stats.py](app/routers/usage_stats.py) |
| Image Processing | ✅ Complete | [app/routers/images.py](app/routers/images.py) |
| Image Deletion | ❌ Missing | N/A |
| Keyboard Shortcuts | ✅ Extensive | [templates/base.html](templates/base.html#L495-L575), pages |
| PWA Support | ❌ Not implemented | N/A |
| Static Assets | ⚠️ Mounted but empty locally | `/app/static` (Docker) |
