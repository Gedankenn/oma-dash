/* fbdash v3 - tabbed cyberpunk console for Control iD iDBoxV2 (480x272 RGB24) */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <math.h>
#include <errno.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <linux/input.h>
#include "font_aa.h"
#include "num_aa.h"   /* Manrope 800, tabular: the big numbers */

#define W 480
#define H 272
#define STRIDE 1440
#define FB_SIZE (STRIDE * H)
#define API "http://192.168.5.245:1985"
#define NTAB 6
#define MAXD 10
#define CW 320
#define CH 180

static unsigned char *fb;
static unsigned char drawbuf[FB_SIZE];
static int rot180 = 1;

static inline void px(int x, int y, int r, int g, int b) {
    if (x < 0 || y < 0 || x >= W || y >= H) return;
    if (rot180) { x = W - 1 - x; y = H - 1 - y; }
    unsigned char *p = drawbuf + y * STRIDE + x * 3;
    p[0] = b; p[1] = g; p[2] = r;
}
static void rect(int x, int y, int w, int h, int r, int g, int b) {
    if (w <= 0 || h <= 0) return;
    for (int j = 0; j < h; j++) for (int i = 0; i < w; i++) px(x + i, y + j, r, g, b);
}
static void line(int x0, int y0, int x1, int y1, int r, int g, int b) {
    int dx = abs(x1 - x0), sx = x0 < x1 ? 1 : -1;
    int dy = -abs(y1 - y0), sy = y0 < y1 ? 1 : -1, err = dx + dy;
    for (;;) {
        px(x0, y0, r, g, b);
        if (x0 == x1 && y0 == y1) break;
        int e2 = 2 * err;
        if (e2 >= dy) { err += dy; x0 += sx; }
        if (e2 <= dx) { err += dx; y0 += sy; }
    }
}
static void frame_rect(int x, int y, int w, int h, int r, int g, int b) {
    rect(x, y, w, 2, r, g, b); rect(x, y + h - 2, w, 2, r, g, b);
    rect(x, y, 2, h, r, g, b); rect(x + w - 2, y, 2, h, r, g, b);
}
static void corners(int x, int y, int w, int h, int l, int r, int g, int b) {
    rect(x, y, l, 2, r, g, b); rect(x, y, 2, l, r, g, b);
    rect(x + w - l, y, l, 2, r, g, b); rect(x + w - 2, y, 2, l, r, g, b);
    rect(x, y + h - 2, l, 2, r, g, b); rect(x, y + h - l, 2, l, r, g, b);
    rect(x + w - l, y + h - 2, l, 2, r, g, b); rect(x + w - 2, y + h - l, 2, l, r, g, b);
}

/* ---------------- antialiased core (text, curves, gauges) ---------------- */
/* The panel is 480x272 with no GPU help: everything here writes 8-bit
   coverage into the BGR framebuffer, so edges get real grays instead of
   stair-steps. The pixel font is kept only for the hero numbers. */

static inline void pxb(int x, int y, int a, int r, int g, int b) {
    if (a <= 0 || x < 0 || y < 0 || x >= W || y >= H) return;
    if (rot180) { x = W - 1 - x; y = H - 1 - y; }
    unsigned char *p = drawbuf + y * STRIDE + x * 3;
    if (a >= 255) { p[0] = b; p[1] = g; p[2] = r; return; }
    p[0] = (unsigned char)((b * a + p[0] * (255 - a)) / 255);
    p[1] = (unsigned char)((g * a + p[1] * (255 - a)) / 255);
    p[2] = (unsigned char)((r * a + p[2] * (255 - a)) / 255);
}

static double clamp01(double v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }

static double cov_circle(double x, double y, double cx, double cy, double rad) {
    double dx = x - cx, dy = y - cy;
    double d = sqrt(dx * dx + dy * dy) - rad;
    return clamp01(0.5 - d);
}

/* signed distance to a rounded rect, coverage over a 1 px edge */
static double cov_rrect(double x, double y, double rx, double ry, double w, double h, double rad) {
    if (rad > w / 2) rad = w / 2;
    if (rad > h / 2) rad = h / 2;
    double cx = rx + w / 2, cy = ry + h / 2;
    double qx = fabs(x - cx) - (w / 2 - rad), qy = fabs(y - cy) - (h / 2 - rad);
    double ox = qx > 0 ? qx : 0, oy = qy > 0 ? qy : 0;
    double d = sqrt(ox * ox + oy * oy) + (qx > qy ? qx : qy) - rad;
    return clamp01(0.5 - d);
}

static void fill_rrect(double x, double y, double w, double h, double rad,
                       int r, int g, int b) {
    if (w <= 0 || h <= 0) return;
    int x0 = (int)floor(x) - 1, x1 = (int)ceil(x + w) + 1;
    int y0 = (int)floor(y) - 1, y1 = (int)ceil(y + h) + 1;
    for (int py = y0; py <= y1; py++)
        for (int px_ = x0; px_ <= x1; px_++) {
            double c = cov_rrect(px_ + 0.5, py + 0.5, x, y, w, h, rad);
            if (c > 0) pxb(px_, py, (int)(c * 255), r, g, b);
        }
}

static void fill_circle(double cx, double cy, double rad, int r, int g, int b) {
    int x0 = (int)floor(cx - rad) - 1, x1 = (int)ceil(cx + rad) + 1;
    int y0 = (int)floor(cy - rad) - 1, y1 = (int)ceil(cy + rad) + 1;
    for (int py = y0; py <= y1; py++)
        for (int px_ = x0; px_ <= x1; px_++) {
            double c = cov_circle(px_ + 0.5, py + 0.5, cx, cy, rad);
            if (c > 0) pxb(px_, py, (int)(c * 255), r, g, b);
        }
}

/* arc from a0 to a1 degrees (0 = 3 o'clock, clockwise), stroked hw wide */
static void arc(double cx, double cy, double rad, double hw, double a0, double a1,
                int r, int g, int b) {
    if (a1 <= a0) return;
    double lo = a0 * M_PI / 180.0, hi = a1 * M_PI / 180.0;
    double R = rad + hw;
    int x0 = (int)floor(cx - R) - 1, x1 = (int)ceil(cx + R) + 1;
    int y0 = (int)floor(cy - R) - 1, y1 = (int)ceil(cy + R) + 1;
    for (int py = y0; py <= y1; py++)
        for (int px_ = x0; px_ <= x1; px_++) {
            double dx = px_ + 0.5 - cx, dy = py + 0.5 - cy;
            double d = sqrt(dx * dx + dy * dy);
            double cov = clamp01(hw + 0.5 - fabs(d - rad));
            if (cov <= 0) continue;
            double ang = atan2(dy, dx);
            if (ang < lo) ang += 2 * M_PI;
            if (ang > hi + 1e-9) {
                if (ang - 2 * M_PI < lo - 1e-9) ang -= 2 * M_PI; else continue;
            }
            if (ang < lo - 1e-9 || ang > hi + 1e-9) continue;
            pxb(px_, py, (int)(cov * 255), r, g, b);
        }
}

/* antialiased line of half-width hw */
static void linea(double x0, double y0, double x1, double y1, double hw,
                  int r, int g, int b) {
    double dx = x1 - x0, dy = y1 - y0;
    double len2 = dx * dx + dy * dy;
    double pad = hw + 1;
    int bx0 = (int)floor((x0 < x1 ? x0 : x1) - pad), bx1 = (int)ceil((x0 > x1 ? x0 : x1) + pad);
    int by0 = (int)floor((y0 < y1 ? y0 : y1) - pad), by1 = (int)ceil((y0 > y1 ? y0 : y1) + pad);
    for (int py = by0; py <= by1; py++)
        for (int px_ = bx0; px_ <= bx1; px_++) {
            double qx = px_ + 0.5 - x0, qy = py + 0.5 - y0;
            double tpar = len2 > 0 ? (qx * dx + qy * dy) / len2 : 0;
            if (tpar < 0) tpar = 0;
            if (tpar > 1) tpar = 1;
            double ex = qx - dx * tpar, ey = qy - dy * tpar;
            double d = sqrt(ex * ex + ey * ey);
            double cov = clamp01(hw + 0.5 - d);
            if (cov > 0) pxb(px_, py, (int)(cov * 255), r, g, b);
        }
}

static int tw(const struct aa_face *f, const char *s) {
    return (int)strlen(s) * f->cellw;
}

/* draw text with y = top of the capital letters */
static int txt(int x, int ytop, const char *s, int r, int g, int b, const struct aa_face *f) {
    int base = ytop + f->caph;
    int cx = x;
    for (; *s; s++, cx += f->cellw) {
        unsigned char c = (unsigned char)*s;
        if (c == ' ') continue;
        if (c < 32 || c > 176) c = '?';      /* atlas covers Latin-1 up to the degree sign */
        const struct aa_glyph *gl = &f->g[c - 32];
        if (!gl->w || !gl->h) continue;
        int gx = cx + gl->xoff, gy = base - gl->yoff;
        const unsigned char *bm = f->bm + gl->off;
        for (int j = 0; j < gl->h; j++)
            for (int i = 0; i < gl->w; i++) {
                int a = bm[j * gl->w + i];
                if (a) pxb(gx + i, gy + j, a, r, g, b);
            }
    }
    return cx - x;
}

/* right-aligned text, x = right edge */
static void txt_r(int xright, int ytop, const char *s, int r, int g, int b,
                  const struct aa_face *f) {
    txt(xright - tw(f, s), ytop, s, r, g, b, f);
}

/* centred text, x = centre */
static void txt_c(int xc, int ytop, const char *s, int r, int g, int b,
                  const struct aa_face *f) {
    txt(xc - tw(f, s) / 2, ytop, s, r, g, b, f);
}

/* ---------------- data ---------------- */
struct dock { char name[20]; int up; float cpu, mem; };
struct dev { int ip; float rtt; };
struct data {
    struct dev v[20]; int vn;
    int pc_ok;
    float pcc, pcm, pcg, pcgt, pcct, pcl, pcpw;
    float gt;                      /* temperatura da GPU do omaserver */
    char gname[24];                /* modelo da GPU do omaserver */
    float nrx, ntx;                /* rede atual, KB/s */
    int dh_ok, dh_n;               /* saude SMART: 1 = todos ok, n = quantos discos */
    float dh_t;                    /* maior temperatura entre os discos */
    char dh_bad[8];                /* dispositivo com falha, se houver */
    int dsk, dsk2;                 /* uso de / e /data, % */
    unsigned short hrx[64], htx[64]; int hnet;
    int pcf[5], pcgf, pcu;
    char pcgn[24];                 /* nome da GPU dedicada, ex. "RX 5600 XT" */
    char pccn[24];                 /* nome da CPU, ex. "Ryzen 5 4600G" */
    unsigned char hpc[64], hpg[64], hpm[64]; int pcn;
    float cpu, mem, temp, load1, load2, load3;
    int up, gpu, dockN, dockU, alive, total;
    struct dock d[MAXD]; int dn;
    int online;
    char clock[16];
    unsigned char hcpu[64]; int hn;
};
static unsigned char matrix_alive[255];

static unsigned char *cambuf[4];
static int camok[4];
static int cam_zoom = -1;
static time_t cam_enter_sec = 0;
static const char *CAMSRC[4] = {"cam", "samsung", "cam3", "cam4"};

static char *fetch(const char *url, int timeout, char *buf, size_t sz) {
    char cmd[256];
    snprintf(cmd, sizeof cmd, "wget -q -T %d -O - '%s' 2>/dev/null", timeout, url);
    FILE *f = popen(cmd, "r");
    if (!f) return NULL;
    size_t n = fread(buf, 1, sz - 1, f);
    pclose(f);
    if (!n) {                       /* busybox wget missing/failed: try curl */
        snprintf(cmd, sizeof cmd, "curl -fsS --max-time %d '%s' 2>/dev/null", timeout, url);
        f = popen(cmd, "r");
        if (!f) return NULL;
        n = fread(buf, 1, sz - 1, f);
        pclose(f);
    }
    buf[n] = 0;
    return n ? buf : NULL;
}

/* ---------------- weather (Open-Meteo via omaserver /weather.txt) ------------- */
#define MAXH 72
struct wxday { char label[6]; int code, tmax, tmin, precip; char sunrise[6], sunset[6]; };
struct wx {
    int ok, have;
    char loc[24], cond[28], sunrise[6], sunset[6];
    int temp, code, hum, wind, feels;
    int hn; int htemp[MAXH], hprecip[MAXH]; char hlabel[MAXH][6]; char hdate[MAXH][11];
    int dn; struct wxday d[3];
};

static void parse_wx(struct wx *w, const char *s) {
    struct wx n;
    char line[160];
    const char *p = s;
    memset(&n, 0, sizeof n);
    while (*p) {
        const char *e = strchr(p, '\n');
        size_t len = e ? (size_t)(e - p) : strlen(p);
        if (len >= sizeof line) len = sizeof line - 1;
        memcpy(line, p, len); line[len] = 0;
        p = e ? e + 1 : p + strlen(p);
        if (!strncmp(line, "ok=", 3)) n.ok = atoi(line + 3);
        else if (!strncmp(line, "loc=", 4)) snprintf(n.loc, sizeof n.loc, "%.23s", line + 4);
        else if (!strncmp(line, "txt=", 4)) snprintf(n.cond, sizeof n.cond, "%.27s", line + 4);
        else if (!strncmp(line, "sun=", 4)) sscanf(line + 4, "%5[^|]|%5s", n.sunrise, n.sunset);
        else if (!strncmp(line, "now=", 4))
            sscanf(line + 4, "%d|%d|%d|%d|%d", &n.temp, &n.code, &n.hum, &n.wind, &n.feels);
        else if (!strncmp(line, "h=", 2) && n.hn < MAXH) {
            char lab[8] = {0}, day[12] = {0};
            int i = n.hn;
            n.htemp[i] = n.hprecip[i] = 0;
            if (sscanf(line + 2, "%7[^|]|%d|%d|%11s", lab, &n.htemp[i], &n.hprecip[i], day) >= 1) {
                snprintf(n.hlabel[i], sizeof n.hlabel[i], "%s", lab);
                snprintf(n.hdate[i], sizeof n.hdate[i], "%s", day);
                n.hn++;
            }
        } else if (!strncmp(line, "d=", 2) && n.dn < 3) {
            struct wxday *dd = &n.d[n.dn];
            memset(dd, 0, sizeof *dd);
            sscanf(line + 2, "%5[^|]|%d|%d|%d|%d|%5[^|]|%5s", dd->label, &dd->code,
                   &dd->tmax, &dd->tmin, &dd->precip, dd->sunrise, dd->sunset);
            n.dn++;
        }
    }
    n.have = 1;
    *w = n;
}

static unsigned char *wxcache = NULL;
static int wxcw = 0, wxch = 0, wxdirty = 1;

/* the panel keeps 64 samples in RAM; on boot that is an empty chart, so pull
   the server's 120-sample history once and preload the tail. */
static void seed_history(struct data *d) {
    static char buf[24000];
    if (!fetch(API "/history", 3, buf, sizeof buf)) return;
    char *p = strstr(buf, "\"cpu\":[");
    if (!p) return;
    p += 7;
    int n = 0;
    while (*p && *p != ']' && n < 64) {
        double v = atof(p);
        if (v < 0) v = 0;
        if (v > 100) v = 100;
        d->hcpu[n++] = (unsigned char)v;
        while (*p && *p != ',' && *p != ']') p++;
        if (*p == ',') p++;
    }
    d->hn = n;
}

static int fetch_wx(struct wx *w) {
    static char buf[8192];
    if (!fetch(API "/weather.txt", 4, buf, sizeof buf)) return 0;
    parse_wx(w, buf);
    wxdirty = 1;
    return w->have;
}

/* --- weather drawing ------------------------------------------------------- */
static void disc(int cx, int cy, int rad, int r, int g, int b) {
    for (int y = -rad; y <= rad; y++)
        for (int x = -rad; x <= rad; x++)
            if (x * x + y * y <= rad * rad) px(cx + x, cy + y, r, g, b);
}

/* WMO weather_code -> icon family, same groups as the omaweather Model.js. */
static int wx_kind(int c) {
    if (c == 0) return 0;
    if (c == 1) return 1;
    if (c == 2) return 2;
    if (c == 3) return 3;
    if (c == 45 || c == 48) return 5;
    if (c >= 51 && c <= 57) return 6;
    if (c >= 61 && c <= 67) return 7;
    if (c >= 71 && c <= 77) return 8;
    if (c >= 80 && c <= 82) return 6;
    if (c == 85 || c == 86) return 8;
    if (c >= 95) return 9;
    return 3;
}

/* Cold-to-hot ramp (blue at -5C, red at 32C), same as tempColor() in Model.js. */
static void wx_temp_rgb(double t, int *r, int *g, int *b) {
    if (t < -5) t = -5;
    if (t > 32) t = 32;
    double f = (t + 5.0) / 37.0;
    double h = (220.0 - f * 220.0) / 60.0;
    double sat = 0.82, lum = 0.58;
    double c = (1 - fabs(2 * lum - 1)) * sat;
    double x = c * (1 - fabs(fmod(h, 2.0) - 1));
    double m = lum - c / 2;
    double rr = 0, gg = 0, bb = 0;
    if (h < 1) { rr = c; gg = x; }
    else if (h < 2) { rr = x; gg = c; }
    else if (h < 3) { gg = c; bb = x; }
    else { gg = x; bb = c; }
    *r = (int)((rr + m) * 255);
    *g = (int)((gg + m) * 255);
    *b = (int)((bb + m) * 255);
}

/* antialiased weather glyphs: union coverage, one blend per pixel */
static void wx_cloud_aa(double x, double y, double w, double h, int r, int g, int b) {
    double rad = h / 3.6;
    double yb = y + h - rad - 0.5;
    int x0 = (int)floor(x) - 1, x1 = (int)ceil(x + w) + 1;
    int y0 = (int)floor(y) - 1, y1 = (int)ceil(y + h) + 1;
    for (int py = y0; py <= y1; py++)
        for (int px_ = x0; px_ <= x1; px_++) {
            double c = cov_circle(px_ + 0.5, py + 0.5, x + rad + 0.5, yb, rad);
            double c2 = cov_circle(px_ + 0.5, py + 0.5, x + w * 0.46, yb - rad * 0.75, rad * 1.12);
            double c3 = cov_circle(px_ + 0.5, py + 0.5, x + w - rad - 0.5, yb, rad * 0.94);
            double c4 = cov_rrect(px_ + 0.5, py + 0.5, x + rad, yb - rad * 0.55, w - rad * 2, rad * 1.55, rad * 0.7);
            if (c2 > c) c = c2;
            if (c3 > c) c = c3;
            if (c4 > c) c = c4;
            if (c > 0) pxb(px_, py, (int)(c * 255), r, g, b);
        }
}

static void wx_sun_aa(double cx, double cy, double rad, double hw, int r, int g, int b) {
    fill_circle(cx, cy, rad, r, g, b);
    for (int a = 0; a < 360; a += 45) {
        double ra = (a + 22.5) * M_PI / 180.0;
        linea(cx + cos(ra) * (rad + 2.2), cy + sin(ra) * (rad + 2.2),
              cx + cos(ra) * (rad + 4.2), cy + sin(ra) * (rad + 4.2), hw, r, g, b);
    }
}

static void wx_icon(int x, int y, int w, int code) {
    int kind = wx_kind(code);
    double s = w, h = w * 0.75;
    int cr = 214, cg = 232, cb = 246;
    if (kind == 3) { cr = 138; cg = 156; cb = 172; }
    if (kind == 5) { cr = 158; cg = 174; cb = 188; }
    if (kind >= 6) { cr = 158; cg = 178; cb = 196; }

    if (kind == 0 || kind == 1) {
        wx_sun_aa(x + s / 2, y + h / 2, s * 0.22, 0.7, 255, 208, 84);
        return;
    }
    if (kind == 2) {
        wx_sun_aa(x + s * 0.32, y + h * 0.26, s * 0.14, 0.6, 255, 208, 84);
        wx_cloud_aa(x + s * 0.12, y + h * 0.30, s * 0.88, h * 0.62, cr, cg, cb);
        return;
    }
    wx_cloud_aa(x, y, s, h * 0.66, cr, cg, cb);
    double by = y + h * 0.70;
    if (kind == 5) {
        linea(x + 2, by + 1.5, x + s - 2, by + 1.5, 0.6, 126, 144, 158);
        linea(x + 4, by + 4.5, x + s - 4, by + 4.5, 0.6, 126, 144, 158);
    } else if (kind == 6 || kind == 7) {
        int n = (kind == 7) ? 3 : 2;
        for (int i = 0; i < n; i++) {
            double dx = x + 3 + i * (s - 7) / (n > 1 ? n - 1 : 1) / 2 * 2;
            linea(dx, by, dx - 1.2, by + h * 0.22, 0.6, 88, 168, 255);
        }
    } else if (kind == 8) {
        for (int i = 0; i < 3; i++)
            fill_circle(x + 5 + i * (s - 10) / 3.0, by + 3, 1.0, 224, 242, 255);
    } else if (kind == 9) {
        double mx = x + s / 2;
        linea(mx, by - 1, mx - 2.6, by + h * 0.13, 0.6, 255, 214, 60);
        linea(mx - 2.6, by + h * 0.13, mx + 1.8, by + h * 0.13, 0.6, 255, 214, 60);
        linea(mx + 1.8, by + h * 0.13, mx - 1.6, by + h * 0.28, 0.6, 255, 214, 60);
    }
}

static int wx_wday(int y, int m, int d) {
    static const int t[] = {0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4};
    if (m < 3) y -= 1;
    return (y + y / 4 - y / 100 + y / 400 + t[m - 1] + d) % 7;     /* 0 = Sunday */
}

/* 72 h temperature curve (coloured by value) + precipitation bars, with the
   three calendar days labelled and divided like the omaweather panel chart.
   Catmull-Rom sampling + coverage lines: no stair-steps, no visible pixels. */
static double wx_ty(struct wx *w, double v, int top, int base, int lo, int hi) {
    (void)w;
    return base - (v - lo) * (double)(base - top - 4) / (hi - lo);
}

static void wx_graph_draw(struct wx *w, int x, int y, int gw, int gh) {
    static const char *DNAMES[7] = {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"};
    char b[8];
    txt_r(x + gw - 4, y + 3, "RAIN %", 74, 100, 122, &AA_FACE_11);
    if (w->hn < 2) {
        txt(x + 4, y + 4, "NO HOURLY DATA", 255, 59, 84, &AA_FACE_11);
        return;
    }

    int top = y + 18, base = y + gh - 15;
    int lo = 999, hi = -999;
    for (int i = 0; i < w->hn; i++) {
        if (w->htemp[i] < lo) lo = w->htemp[i];
        if (w->htemp[i] > hi) hi = w->htemp[i];
    }
    if (hi - lo < 2) hi = lo + 2;

    frame_rect(x, y, gw, gh, 12, 40, 54);
    for (int g = 0; g <= 2; g++) {
        int gy = top + (base - top) * g / 2;
        for (int gx = x + 6; gx < x + gw - 6; gx++) pxb(gx, gy, 22, 16, 34, 44);
    }

    int x0 = x + 9, x1 = x + gw - 9;
    double slot = (double)(x1 - x0) / (w->hn - 1);

    /* precipitation: capsule bars, 44% of the plot, soft edges */
    double maxBar = (base - top) * 0.44;
    for (int i = 0; i < w->hn; i++) {
        int p = w->hprecip[i];
        if (p <= 0) continue;
        double bh = p * maxBar / 100.0;
        if (bh < 1.5) bh = 1.5;
        double bx = x0 + i * slot;
        int al = p >= 40 ? 1 : 0.72;
        if (al == 1) {
            fill_rrect(bx - 0.6, base - bh, 2.8, bh, 1.3, 96, 164, 226);
            for (int k = 0; k < 3; k++) pxb((int)(bx), base - (int)bh + k, 120, 104, 200, 255);
        } else {
            fill_rrect(bx - 0.6, base - bh, 2.8, bh, 1.3, 54, 116, 172);
        }
    }

    /* day separators + labels, from the per-hour date */
    int s0 = 0;
    for (int i = 1; i <= w->hn; i++) {
        int boundary = (i == w->hn) || strcmp(w->hdate[i], w->hdate[s0]) != 0;
        if (!boundary) continue;
        int xs = x0 + (int)(s0 * slot);
        int xe = x0 + (int)((i - 1) * slot);
        if (s0 > 0) {
            for (int yy = top; yy < base; yy++) pxb(xs, yy, 60, 34, 72, 90);
        }
        if (xe - xs >= 34 && w->hdate[s0][0]) {
            int yy = 0, mm = 0, dd = 0;
            if (sscanf(w->hdate[s0], "%4d-%2d-%2d", &yy, &mm, &dd) == 3) {
                const char *lab = DNAMES[wx_wday(yy, mm, dd)];
                txt_c((xs + xe) / 2, y + 4, lab, 116, 196, 220, &AA_FACE_11);
            }
        }
        s0 = i;
    }

    /* soft area under the curve */
    for (int i = 0; i <= x1 - x0; i++) {
        double tpos = i / slot;
        int i0 = (int)tpos;
        if (i0 >= w->hn - 1) i0 = w->hn - 2;
        double fr = tpos - i0;
        double v = w->htemp[i0] + (w->htemp[i0 + 1] - w->htemp[i0]) * fr;
        int ty = (int)wx_ty(w, v, top, base, lo, hi);
        for (int yy = ty; yy < base; yy++) {
            int a = 46 - (yy - ty) * 46 / (base - ty + 1);
            if (a > 0) pxb(x0 + i, yy, a, 40, 170, 210);
        }
    }

    /* temperature curve: dense Catmull-Rom sampling, per-segment colour */
    double px0 = x0, py0 = wx_ty(w, w->htemp[0], top, base, lo, hi);
    for (int i = 0; i < w->hn - 1; i++) {
        double p0 = w->htemp[i > 0 ? i - 1 : i];
        double p1 = w->htemp[i], p2 = w->htemp[i + 1];
        double p3 = w->htemp[i + 2 < w->hn ? i + 2 : i + 1];
        int steps = 6;
        for (int k = 1; k <= steps; k++) {
            double t = (double)k / steps;
            double t2 = t * t, t3 = t2 * t;
            double v = 0.5 * ((2 * p1) + (-p0 + p2) * t
                              + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3);
            double nx = x0 + (i + t) * slot;
            double ny = wx_ty(w, v, top, base, lo, hi);
            int r, g, bl;
            wx_temp_rgb(v, &r, &g, &bl);
            linea(px0, py0, nx, ny, 1.0, r, g, bl);
            px0 = nx; py0 = ny;
        }
    }

    /* now marker + leading dot */
    for (int yy = top; yy < base; yy += 2) pxb(x0, yy, 190, 255, 140, 0);
    fill_circle(x0, wx_ty(w, w->htemp[0], top, base, lo, hi), 2.4, 255, 255, 255);

    /* hour ticks every 6 h */
    for (int i = 0; i < w->hn; i += 6) {
        b[0] = w->hlabel[i][0];
        b[1] = w->hlabel[i][1];
        b[2] = 0;
        txt_c(x0 + (int)(i * slot), base + 3, b, 96, 116, 136, &AA_FACE_11);
    }
}

static void wx_graph(struct wx *w, int x, int y, int gw, int gh) {
    if (!wxcache || wxcw != gw || wxch != gh) {
        free(wxcache);
        wxcache = malloc((size_t)gw * gh * 3);
        wxcw = gw; wxch = gh;
        wxdirty = 1;
    }
    /* the 180 deg rotation maps a logical block onto a physical block, so the
       cache has to be taken in framebuffer coordinates, not logical ones */
    int px0 = rot180 ? W - x - gw : x;
    int py0 = rot180 ? H - y - gh : y;
    if (!wxcache || wxdirty) {
        wx_graph_draw(w, x, y, gw, gh);
        for (int j = 0; j < gh; j++)
            memcpy(wxcache + (size_t)j * gw * 3,
                   drawbuf + (size_t)(py0 + j) * STRIDE + px0 * 3, gw * 3);
        wxdirty = 0;
        return;
    }
    for (int j = 0; j < gh; j++)
        memcpy(drawbuf + (size_t)(py0 + j) * STRIDE + px0 * 3, wxcache + (size_t)j * gw * 3, gw * 3);
}

static void tab_wx(struct wx *w, int t) {
    char b[64];
    txt(64, 26, "WEATHER", 34, 224, 255, &AA_FACE_13);
    if (w->loc[0]) txt(64 + tw(&AA_FACE_13, "WEATHER") + 8, 26, w->loc, 200, 233, 245, &AA_FACE_11);
    if (w->sunrise[0]) {
        snprintf(b, sizeof b, "UP %s DN %s", w->sunrise, w->sunset);
        txt(472 - (int)strlen(b) * 7, 26, b, 120, 150, 170, &AA_FACE_11);
    }
    if (!w->have || !w->ok) {
        txt(64, 70, "NO WEATHER FEED", 255, 59, 84, &AA_FACE_11);
        txt(64, 86, "API 192.168.5.245:1985", 60, 90, 110, &AA_FACE_11);
        return;
    }

    snprintf(b, sizeof b, "%d", w->temp);
    txt(64, 37, b, 255, 255, 255, &AA_FACE_NUM_40);
    txt(64 + tw(&AA_FACE_NUM_40, b) + 4, 43, "C", 34, 224, 255, &AA_FACE_16);
    txt(64, 74, w->cond, 200, 233, 245, &AA_FACE_11);
    snprintf(b, sizeof b, "HUM %d%%  WIND %dKMH", w->hum, w->wind);
    txt(64, 88, b, 90, 130, 150, &AA_FACE_11);
    snprintf(b, sizeof b, "FEELS %d\xB0" "C", w->feels);
    txt(64, 100, b, 90, 130, 150, &AA_FACE_11);

    for (int i = 0; i < w->dn && i < 3; i++) {
        struct wxday *d = &w->d[i];
        int cx = 248 + i * 76, cy = 38, cw = 70, ch = 60;
        frame_rect(cx, cy, cw, ch, 18, 60, 78);
        rect(cx, cy, cw, 12, 10, 22, 32);
        txt(cx + 4, cy + 3, d->label, 34, 224, 255, &AA_FACE_11);
        wx_icon(cx + 4, cy + 15, 23, d->code);
        snprintf(b, sizeof b, "%d/%d", d->tmax, d->tmin);
        txt(cx + 30, cy + 19, b, 245, 250, 255, &AA_FACE_13);
        snprintf(b, sizeof b, "R %d%%", d->precip);
        txt(cx + 30, cy + 36, b, d->precip >= 60 ? 70 : 90, d->precip >= 60 ? 150 : 130,
             d->precip >= 60 ? 255 : 150, &AA_FACE_11);
    }

    (void)t;
    wx_graph(w, 64, 116, 406, 122);
}

/* nome vindo do feed: so ASCII imprimivel, para a face de 95 glifos */
static void feed_name(char *dst, size_t n, const char *src) {
    size_t k = 0;
    for (const char *q = src; *q && k < n - 1; q++)
        if ((unsigned char)*q >= 0x20 && (unsigned char)*q < 0x7f)
            dst[k++] = *q;
    dst[k] = 0;
}

static void parse_feed(struct data *d, const char *s) {
    char line[256];
    const char *p = s;
    memset(d->d, 0, sizeof d->d);
    d->dn = 0;
    d->vn = 0;
    d->online = 1;
    memset(matrix_alive, 0, sizeof matrix_alive);
    while (*p) {
        const char *e = strchr(p, '\n');
        size_t n = e ? (size_t)(e - p) : strlen(p);
        if (n >= sizeof line) n = sizeof line - 1;
        memcpy(line, p, n); line[n] = 0;
        p = e ? e + 1 : p + strlen(p);
        if (!strncmp(line, "cpu=", 4)) d->cpu = atof(line + 4);
        else if (!strncmp(line, "mem=", 4)) d->mem = atof(line + 4);
        else if (!strncmp(line, "temp=", 5)) d->temp = atof(line + 5);
        else if (!strncmp(line, "load=", 5)) sscanf(line + 5, "%f,%f,%f", &d->load1, &d->load2, &d->load3);
        else if (!strncmp(line, "up=", 3)) d->up = atoi(line + 3);
        else if (!strncmp(line, "gpu=", 4)) d->gpu = atoi(line + 4);
        else if (!strncmp(line, "gt=", 3)) d->gt = atof(line + 3);
        else if (!strncmp(line, "rx=", 3)) d->nrx = atof(line + 3);
        else if (!strncmp(line, "tx=", 3)) d->ntx = atof(line + 3);
        else if (!strncmp(line, "disk=", 5)) sscanf(line + 5, "%d|%d", &d->dsk, &d->dsk2);
        else if (!strncmp(line, "dh=", 3)) {
            char bad[8] = "";
            sscanf(line + 3, "%d|%d|%f|%7s", &d->dh_ok, &d->dh_n, &d->dh_t, bad);
            snprintf(d->dh_bad, sizeof d->dh_bad, "%s", bad);
        }
        else if (!strncmp(line, "gname=", 6)) feed_name(d->gname, sizeof d->gname, line + 6);
        else if (!strncmp(line, "dock=", 5)) sscanf(line + 5, "%d|%d", &d->dockN, &d->dockU);
        else if (!strncmp(line, "alive=", 6)) sscanf(line + 6, "%d|%d", &d->alive, &d->total);
        else if (!strncmp(line, "d=", 2) && d->dn < MAXD) {
            struct dock *dk = &d->d[d->dn];
            sscanf(line + 2, "%19[^|]|%d|%f|%f", dk->name, &dk->up, &dk->cpu, &dk->mem);
            d->dn++;
        } else if (!strncmp(line, "ts=", 3)) {
            time_t st = (time_t)(atol(line + 3) - 3 * 3600);
            struct tm *gt = gmtime(&st);
            strftime(d->clock, sizeof d->clock, "%H:%M:%S", gt);
        } else if (!strncmp(line, "n=", 2)) {
            int ip = atoi(line + 2);
            if (ip > 0 && ip < 255) matrix_alive[ip] = 1;
        } else if (!strncmp(line, "pg=", 3)) {
            feed_name(d->pcgn, sizeof d->pcgn, line + 3);
        } else if (!strncmp(line, "pcn=", 4)) {
            feed_name(d->pccn, sizeof d->pccn, line + 4);
        } else if (!strncmp(line, "p=", 2)) {
            if (!strncmp(line + 2, "off", 3)) {
                d->pc_ok = 0;
            } else {
                int f0, f1, f2, f3, f4, gf, up;
                float c, m, g, gt, ct, pw, ld;
                if (sscanf(line + 2, "%f|%f|%f|%f|%f|%d|%d|%d|%d|%d|%d|%f|%f|%d",
                           &c, &m, &g, &gt, &ct, &f0, &f1, &f2, &f3, &f4,
                           &gf, &pw, &ld, &up) == 14) {
                    d->pc_ok = 1;
                    d->pcc = c; d->pcm = m; d->pcg = g; d->pcgt = gt; d->pcct = ct;
                    d->pcf[0] = f0; d->pcf[1] = f1; d->pcf[2] = f2; d->pcf[3] = f3; d->pcf[4] = f4;
                    d->pcgf = gf; d->pcpw = pw; d->pcl = ld; d->pcu = up;
                    if (d->pcn < 64) {
                        d->hpc[d->pcn] = (unsigned char)c;
                        d->hpg[d->pcn] = (unsigned char)g;
                        d->hpm[d->pcn] = (unsigned char)m;
                        d->pcn++;
                    } else {
                        memmove(d->hpc, d->hpc + 1, 63);
                        memmove(d->hpg, d->hpg + 1, 63);
                        memmove(d->hpm, d->hpm + 1, 63);
                        d->hpc[63] = (unsigned char)c;
                        d->hpg[63] = (unsigned char)g;
                        d->hpm[63] = (unsigned char)m;
                    }
                }
            }
        } else if (!strncmp(line, "v=", 2) && d->vn < 20) {
            sscanf(line + 2, "%d|%f", &d->v[d->vn].ip, &d->v[d->vn].rtt);
            d->vn++;
        }
    }
    if (d->hn < 64) d->hcpu[d->hn++] = (unsigned char)d->cpu;
    else { memmove(d->hcpu, d->hcpu + 1, 63); d->hcpu[63] = (unsigned char)d->cpu; }

    unsigned short rx = d->nrx > 65535 ? 65535 : (unsigned short)d->nrx;
    unsigned short tx = d->ntx > 65535 ? 65535 : (unsigned short)d->ntx;
    if (d->hnet < 64) {
        d->hrx[d->hnet] = rx;
        d->htx[d->hnet] = tx;
        d->hnet++;
    } else {
        memmove(d->hrx, d->hrx + 1, 63 * sizeof(unsigned short));
        memmove(d->htx, d->htx + 1, 63 * sizeof(unsigned short));
        d->hrx[63] = rx;
        d->htx[63] = tx;
    }
}

static void collect(struct data *d) {
    static char buf[16384];
    struct data old = *d;
    memset(d, 0, sizeof *d);
    d->hn = old.hn;
    memcpy(d->hcpu, old.hcpu, sizeof d->hcpu);
    d->hnet = old.hnet;
    memcpy(d->hrx, old.hrx, sizeof d->hrx);
    memcpy(d->htx, old.htx, sizeof d->htx);
    d->pcn = old.pcn;
    memcpy(d->hpc, old.hpc, sizeof d->hpc);
    memcpy(d->hpg, old.hpg, sizeof d->hpg);
    memcpy(d->hpm, old.hpm, sizeof d->hpm);
    if (!fetch(API "/screen.txt", 2, buf, sizeof buf)) {
        d->online = 0;
        strcpy(d->clock, "--:--:--");
        return;
    }
    parse_feed(d, buf);
}

static void launch_cams(void) {
    char cmd[320];
    for (int i = 0; i < 4; i++) {
        snprintf(cmd, sizeof cmd,
                 "(wget -q -T 5 -O /tmp/cam%d.tmp '%s/cam/%s.rgb?w=%d&h=%d' && mv /tmp/cam%d.tmp /tmp/cam%d.rgb || rm -f /tmp/cam%d.tmp) &",
                 i, API, CAMSRC[i], CW, CH, i, i, i);
        system(cmd);
    }
}

static void load_cams(void) {
    for (int i = 0; i < 4; i++) {
        char path[32];
        snprintf(path, sizeof path, "/tmp/cam%d.rgb", i);
        struct stat stt;
        camok[i] = 0;
        if (stat(path, &stt) != 0 || time(NULL) - stt.st_mtime > 20) continue;
        FILE *f = fopen(path, "rb");
        if (!f) continue;
        size_t n = fread(cambuf[i], 1, CW * CH * 3, f);
        fclose(f);
        if (n == CW * CH * 3) camok[i] = 1;
    }
}

/* ---------------- touch + 9-point quadratic calibration ---------------- */
static int touchfd = -1;
static int rawx = 0, rawy = 0, touched = 0, tap = 0;
static double kx[6], ky[6];          /* quadratic map: s = k0 +k1x +k2y +k3xy +k4x^2 +k5y^2 */
static int cal_ok = 0;
static int cal_rot = 0;

static void cal_load(void) {
    FILE *f = fopen("/mnt/fs_user/touchcal", "r");
    if (!f) return;
    int ver = 0;
    if (fscanf(f, "%d", &ver) == 1 && ver == 3) {
        int ok = 1;
        for (int i = 0; i < 6; i++) if (fscanf(f, "%lf", &kx[i]) != 1) ok = 0;
        for (int i = 0; i < 6; i++) if (fscanf(f, "%lf", &ky[i]) != 1) ok = 0;
        if (fscanf(f, "%d", &cal_rot) != 1) cal_rot = 0;
        cal_ok = ok;
    }
    fclose(f);
}

static void cal_save(void) {
    FILE *f = fopen("/mnt/fs_user/touchcal", "w");
    if (!f) return;
    fprintf(f, "3\n");
    for (int i = 0; i < 6; i++) fprintf(f, "%.10f ", kx[i]);
    fprintf(f, "\n");
    for (int i = 0; i < 6; i++) fprintf(f, "%.10f ", ky[i]);
    fprintf(f, "\n%d\n", cal_rot);
    fclose(f);
}

static void touch_open(void) {
    touchfd = open("/dev/input/event0", O_RDONLY | O_NONBLOCK);
}

static void touch_poll(void) {
    if (touchfd < 0) return;
    struct input_event ev[32];
    ssize_t n;
    tap = 0;
    while ((n = read(touchfd, ev, sizeof ev)) > 0) {
        int cnt = (int)(n / sizeof(struct input_event));
        for (int i = 0; i < cnt; i++) {
            if (ev[i].type == EV_ABS) {
                if (ev[i].code == ABS_X || ev[i].code == ABS_MT_POSITION_X) rawx = ev[i].value;
                if (ev[i].code == ABS_Y || ev[i].code == ABS_MT_POSITION_Y) rawy = ev[i].value;
            } else if (ev[i].type == EV_KEY && (ev[i].code == BTN_TOUCH || ev[i].code == BTN_LEFT)) {
                touched = ev[i].value;
            } else if (ev[i].type == EV_SYN && ev[i].code == SYN_REPORT) {
                if (touched) tap = 1;
                touched = 0;
            }
        }
    }
}

static double feval(const double *k, double rx, double ry) {
    return k[0] + k[1] * rx + k[2] * ry + k[3] * rx * ry + k[4] * rx * rx + k[5] * ry * ry;
}

static void touch_map(int *x, int *y) {
    double sx = feval(kx, rawx, rawy);
    double sy = feval(ky, rawx, rawy);
    int ix = (int)(sx + 0.5), iy = (int)(sy + 0.5);
    if (rot180 != cal_rot) { ix = W - 1 - ix; iy = H - 1 - iy; }
    if (ix < 0) ix = 0;
    if (ix > W - 1) ix = W - 1;
    if (iy < 0) iy = 0;
    if (iy > H - 1) iy = H - 1;
    *x = ix; *y = iy;
}

static int solve_n(int n, double A[6][7], double x[6]) {
    for (int c = 0; c < n; c++) {
        int p = c;
        for (int r = c + 1; r < n; r++)
            if (fabs(A[r][c]) > fabs(A[p][c])) p = r;
        if (fabs(A[p][c]) < 1e-12) return 0;
        if (p != c) for (int k = 0; k <= n; k++) { double t = A[c][k]; A[c][k] = A[p][k]; A[p][k] = t; }
        double d = A[c][c];
        for (int k = c; k <= n; k++) A[c][k] /= d;
        for (int r = 0; r < n; r++) {
            if (r == c) continue;
            double f = A[r][c];
            if (f == 0) continue;
            for (int k = c; k <= n; k++) A[r][k] -= f * A[c][k];
        }
    }
    for (int i = 0; i < n; i++) x[i] = A[i][n];
    return 1;
}

static int wait_touch_tap(int timeout_frames) {
    for (int i = 0; i < timeout_frames; i++) {
        touch_poll();
        if (tap) return 1;
        usleep(20000);
    }
    return 0;
}

static void sample_press(int *sx, int *sy) {
    long ax = 0, ay = 0;
    int n = 0;
    for (int i = 0; i < 14; i++) {
        touch_poll();
        ax += rawx; ay += rawy; n++;
        usleep(12000);
    }
    *sx = (int)(ax / n);
    *sy = (int)(ay / n);
}

static void calib_frame(const char *msg, int idx, int tx, int ty, int t) {
    rect(0, 0, W, H, 5, 8, 14);
    for (int i = 0; i < 8; i++) {
        int y = (t * 4 + i * 20) % H;
        for (int x = 0; x < W; x++) px(x, y, 4, 16, 24);
    }
    txt(24, 20, "TOUCH CALIBRATION", 34, 224, 255, &AA_FACE_16);
    char b[40];
    snprintf(b, sizeof b, "TARGET %d/9", idx + 1);
    txt(24, 44, b, 255, 47, 214, &AA_FACE_11);
    txt(24, H - 34, msg, 200, 233, 245, &AA_FACE_11);

    for (int a = 0; a < 360; a += 4) {
        double ra = a * M_PI / 180.0;
        rect(tx + (int)(cos(ra) * 14), ty + (int)(sin(ra) * 14), 2, 2, 255, 255, 255);
    }
    rect(tx - 22, ty - 1, 44, 3, 34, 224, 255);
    rect(tx - 1, ty - 22, 3, 44, 34, 224, 255);
    int pulse = (t / 10) % 12;
    rect(tx - 4 - pulse / 3, ty - 4 - pulse / 3, 8 + pulse, 8 + pulse, 255, 47, 214);
    rect(tx - 3, ty - 3, 6, 6, 255, 255, 255);
    memcpy(fb, drawbuf, FB_SIZE);
    msync(fb, FB_SIZE, MS_ASYNC);
}

static void run_calibration(void) {
    if (touchfd < 0) return;
    const int TX[9] = { 60, 240, 420,  60, 240, 420,  60, 240, 420 };
    const int TY[9] = { 60,  60,  60, 136, 136, 136, 212, 212, 212 };
    double AtA[6][6] = {{0}}, Atx[6] = {0}, Aty[6] = {0};
    int t = 0;

    for (int k = 0; k < 9; k++) {
        char msg[64];
        snprintf(msg, sizeof msg, "TAP THE CROSSHAIR  (%d/9)", k + 1);
        touch_poll();
        tap = 0;
        while (1) {
            calib_frame(msg, k, TX[k], TY[k], t++);
            touch_poll();
            if (tap) break;
            usleep(20000);
        }
        int rx, ry;
        sample_press(&rx, &ry);
        double f[6] = { 1.0, (double)rx, (double)ry, (double)rx * ry,
                        (double)rx * rx, (double)ry * ry };
        for (int i = 0; i < 6; i++) {
            for (int j = 0; j < 6; j++) AtA[i][j] += f[i] * f[j];
            Atx[i] += f[i] * (double)TX[k];
            Aty[i] += f[i] * (double)TY[k];
        }
        usleep(250000);
        tap = 0;
    }

    double M[6][7];
    for (int i = 0; i < 6; i++) {
        for (int j = 0; j < 6; j++) M[i][j] = AtA[i][j];
        M[i][6] = Atx[i];
    }
    int okx = solve_n(6, M, kx);
    for (int i = 0; i < 6; i++) {
        for (int j = 0; j < 6; j++) M[i][j] = AtA[i][j];
        M[i][6] = Aty[i];
    }
    int oky = solve_n(6, M, ky);
    if (okx && oky) { cal_ok = 1; cal_rot = rot180; cal_save(); }
}

/* ---------------- drawing helpers ---------------- */
/* smooth capsule bar: no blocks, real grays on the ends */
static void segbar(int x, int y, int w, int h, double pct, int r, int g, int b) {
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    double rad = h / 2.0;
    int fw = (int)(w * pct / 100.0 + 0.5);
    fill_rrect(x, y, w, h, rad, 14, 22, 32);
    if (fw >= 2) fill_rrect(x, y, fw, h, rad, r, g, b);
    else if (fw == 1) rect(x, y + 1, 1, h - 2, r, g, b);
    /* 25 % ticks, 1 px, only over the trough */
    for (int i = 1; i < 4; i++) {
        int tx = x + (int)(w * i / 4.0);
        if (tx > x + 2 && tx < x + w - 2)
            for (int j = y + 2; j < y + h - 2; j++) pxb(tx, j, 40, 6, 10, 16);
    }
}

static void ringg(int cx, int cy, int rad, double pct, int t, int r, int g, int b) {
    (void)t;
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    arc(cx, cy, rad, 2.6, -90, 270, 16, 30, 40);
    arc(cx, cy, rad, 2.6, -90, -90 + 3.6 * pct, r, g, b);
}

static void line_w(int x0, int y0, int x1, int y1, int r, int g, int b, int w) {
    linea(x0, y0, x1, y1, w / 2.0 + 0.5, r, g, b);
}

static void spark(int x, int y, int w, int h, unsigned char *hist, int n, int r, int g, int b, int maxv) {
    if (n < 2) return;
    if (maxv < 10) maxv = 10;
    double px0 = x, py0 = y + h - (hist[0] * (double)h / maxv);
    for (int i = 1; i < n; i++) {
        double x1 = x + i * (double)w / (n - 1);
        double y1 = y + h - (hist[i] * (double)h / maxv);
        linea(px0, py0, x1, y1, 0.9, r, g, b);
        px0 = x1; py0 = y1;
    }
    fill_circle(px0, py0, 1.8, 255, 255, 255);
}

static void spark16(int x, int y, int w, int h, unsigned short *hist, int n,
                    int r, int g, int b, int maxv) {
    if (n < 2) return;
    if (maxv < 10) maxv = 10;
    double px0 = x, py0 = y + h - (hist[0] * (double)h / maxv);
    for (int i = 1; i < n; i++) {
        double x1 = x + i * (double)w / (n - 1);
        double y1 = y + h - (hist[i] * (double)h / maxv);
        if (y1 < y) y1 = y;
        if (y1 > y + h) y1 = y + h;
        line((int)px0, (int)py0, (int)x1, (int)y1, r, g, b);
        px0 = x1; py0 = y1;
    }
    fill_circle((int)px0, (int)py0, 1.6, r, g, b);
}

static void cur_label(int x, int y, int w, int h, unsigned char *hist, int n, int maxv,
                      int r, int g, int b) {
    if (n < 2) return;
    if (maxv < 10) maxv = 10;
    char buf[12];
    int cur = hist[n - 1];
    int cy = y + h - cur * h / maxv;
    snprintf(buf, sizeof buf, "%d", cur);
    int tx = x + w - tw(&AA_FACE_11, buf) - 8;
    int ty = cy - 13;
    if (ty < y) ty = y;
    if (ty > y + h - 9) ty = y + h - 9;
    if (tx < x) tx = x;
    txt(tx, ty, buf, r, g, b, &AA_FACE_11);
}

static void peakval(unsigned char *hist, int n, int *pk, int *vl) {
    int ma = 0, mi = 0;
    for (int i = 1; i < n; i++) {
        if (hist[i] > hist[ma]) ma = i;
        if (hist[i] < hist[mi]) mi = i;
    }
    *pk = hist[ma];
    *vl = hist[mi];
}

static void bg(int t) {
    rect(0, 0, W, H, 4, 6, 11);
    int horizon = 150;
    for (int i = 0; i < 10; i++) {
        double k = i + ((t / 3) % 20) / 20.0;
        int y = horizon + (int)(k * k * 1.4);
        if (y >= H) break;
        int a = 7 + i * 2;
        for (int x = 0; x < W; x++) px(x, y, 4, a, a * 1.7);
    }
    for (int i = -10; i <= 10; i++)
        line(W / 2, horizon, W / 2 + i * 50, H - 1, 3, 13, 22);
    line(0, horizon, W, horizon, 5, 18, 28);
    int sy = ((t / 15) * 5) % (H + 40) - 20;
    for (int y = sy - 3; y <= sy + 3; y++) {
        int dd = y - sy, a = 26 - abs(dd) * 6;
        if (a < 4) continue;
        for (int x = 0; x < W; x++) px(x, y, 4, a, a + 6);
    }
    if (t % 600 < 2)
        for (int k = 0; k < 3; k++) {
            int y = rand() % H, h = 1 + rand() % 3, w = 40 + rand() % 160, x = rand() % (W - w);
            rect(x, y, w, h, rand() % 2 ? 34 : 24, 0, 18);
        }
}

/* ---------------- tabs ---------------- */
#define RAILY 22
#define RAILP 41
static const char *TABS[NTAB] = {"SYS", "PC", "DOCK", "NET", "CAM", "WX"};

static void draw_header(struct data *d, int t) {
    rect(0, 0, W, 20, 5, 8, 14);
    int jit = (t % 600 < 2) ? (rand() % 3 - 1) : 0;
    txt(4 + jit, 5, "OMA", 255, 47, 214, &AA_FACE_16);
    txt(44 + jit, 5, "//", 34, 224, 255, &AA_FACE_16);
    txt(73 + jit, 5, "DASH", 245, 250, 255, &AA_FACE_16);
    txt_r(W - 14, 6, d->clock, 34, 224, 255, &AA_FACE_11);
    rect(W - 8, 8, 3, 3, d->online ? 125 : 255, d->online ? 255 : 59, d->online ? 94 : 84);
    line(0, 20, W, 20, 34, 224, 255);
}

static void draw_rail(int cur, int t) {
    rect(0, 20, 56, H - 20, 6, 9, 16);
    line(56, 20, 56, H - 1, 18, 60, 78);
    for (int i = 0; i < NTAB; i++) {
        int y = RAILY + i * RAILP;
        if (i == cur) {
            rect(2, y, 52, RAILP - 3, 30, 8, 36);
            rect(58 - 2, y, 2, RAILP - 3, 34, 224, 255);
            txt(8, y + 7, TABS[i], 255, 47, 214, &AA_FACE_11);
            txt(8, y + 19, ">", 34, 224, 255, &AA_FACE_11);
        } else {
            txt(8, y + 7, TABS[i], 90, 110, 130, &AA_FACE_11);
        }
        line(4, y + RAILP - 2, 52, y + RAILP - 2, 12, 30, 40);
    }
}

static void tab_sys(struct data *d, int t) {
    char b[64];
    txt(64, 26, "OMASERVER TELEMETRY", 34, 224, 255, &AA_FACE_13);
    if (d->gname[0]) {
        txt(64, 42, "GPU", 60, 90, 110, &AA_FACE_11);
        txt(64 + tw(&AA_FACE_11, "GPU "), 42, d->gname, 157, 123, 255, &AA_FACE_11);
    }

    int cy = 86;
    int gpu = d->gpu < 0 ? 0 : d->gpu;
    ringg(96, cy, 30, d->cpu, t, 34, 224, 255);
    snprintf(b, sizeof b, "%d", (int)d->cpu);
    txt_c(96, cy - 9, b, 255, 255, 255, &AA_FACE_NUM_22);
    txt(96 - 12, cy + 12, "CPU", 60, 90, 110, &AA_FACE_11);

    ringg(186, cy, 30, gpu, t, 157, 123, 255);
    snprintf(b, sizeof b, "%d", gpu);
    txt_c(186, cy - 9, b, 255, 255, 255, &AA_FACE_NUM_22);
    txt(186 - 12, cy + 12, "GPU", 60, 90, 110, &AA_FACE_11);

    int bx = 236;
    snprintf(b, sizeof b, "RAM %3d%%", (int)d->mem);
    txt(bx, 36, b, 200, 233, 245, &AA_FACE_11);
    segbar(bx, 46, 232, 8, d->mem, 34, 224, 255);
    if (d->gt > 0)
        snprintf(b, sizeof b, "CPU %.0f\xB0" "C  GPU %.0f\xB0" "C", d->temp, d->gt);
    else
        snprintf(b, sizeof b, "CPU %.0f\xB0" "C  GPU --", d->temp);
    txt(bx, 58, b, d->temp > 80 ? 255 : 200, d->temp > 80 ? 59 : 233, d->temp > 80 ? 84 : 245, &AA_FACE_11);
    snprintf(b, sizeof b, "LOAD %.2f %.2f %.2f", d->load1, d->load2, d->load3);
    txt(bx, 70, b, 125, 255, 94, &AA_FACE_11);
    snprintf(b, sizeof b, "DISK / %d%%  /data %d%%", d->dsk, d->dsk2);
    txt(bx, 82, b, d->dsk > 90 ? 255 : 200, d->dsk > 90 ? 59 : 233, d->dsk > 90 ? 84 : 245, &AA_FACE_11);
    if (d->dh_n > 0 && d->dh_ok)
        snprintf(b, sizeof b, "SMART OK %dd %.0f\xB0" "C", d->dh_n, d->dh_t);
    else if (d->dh_n > 0)
        snprintf(b, sizeof b, "SMART FALHA %s", d->dh_bad);
    else
        snprintf(b, sizeof b, "SMART --");
    txt(bx, 94, b, d->dh_ok ? 125 : 255, d->dh_ok ? 255 : 59, d->dh_ok ? 94 : 84, &AA_FACE_11);
    snprintf(b, sizeof b, "UP %dd%02dh  DOCK %d/%d", d->up / 86400, (d->up / 3600) % 24,
             d->dockU, d->dockN);
    txt(bx, 106, b, d->dockU == d->dockN ? 200 : 255, d->dockU == d->dockN ? 233 : 59,
        d->dockU == d->dockN ? 245 : 84, &AA_FACE_11);

    /* grafico 1: CPU */
    frame_rect(64, 120, 404, 50, 12, 40, 54);
    txt(68, 123, "CPU LOAD", 60, 90, 110, &AA_FACE_11);
    int mx = 10;
    for (int i = 0; i < d->hn; i++) if (d->hcpu[i] > mx) mx = d->hcpu[i];
    mx = mx * 14 / 10;
    if (d->hn > 1) {
        int pk, vl;
        char pb[40];
        peakval(d->hcpu, d->hn, &pk, &vl);
        snprintf(pb, sizeof pb, "MAX %d%%   MIN %d%%", pk, vl);
        txt_r(464, 123, pb, 90, 130, 150, &AA_FACE_11);
    }
    spark(68, 134, 396, 32, d->hcpu, d->hn, 34, 224, 255, mx);
    cur_label(68, 134, 396, 32, d->hcpu, d->hn, mx, 34, 224, 255);

    /* grafico 2: rede (RX ciano, TX magenta) */
    frame_rect(64, 174, 404, 56, 12, 40, 54);
    txt(68, 177, "NET KB/s", 60, 90, 110, &AA_FACE_11);
    int nmx = 50;
    for (int i = 0; i < d->hnet; i++) {
        if (d->hrx[i] > nmx) nmx = d->hrx[i];
        if (d->htx[i] > nmx) nmx = d->htx[i];
    }
    nmx = nmx * 13 / 10;
    snprintf(b, sizeof b, "RX %d  TX %d", (int)d->nrx, (int)d->ntx);
    txt_r(464, 177, b, 90, 130, 150, &AA_FACE_11);
    spark16(68, 190, 396, 36, d->hrx, d->hnet, 34, 224, 255, nmx);
    spark16(68, 190, 396, 36, d->htx, d->hnet, 255, 47, 214, nmx);
}

static void tab_pc(struct data *d, int t) {
    char b[80];
    txt(64, 26, "SABINHO PC", 157, 123, 255, &AA_FACE_13);
    txt_r(472, 26, "192.168.5.114", 90, 110, 130, &AA_FACE_11);

    if (!d->pc_ok) {
        rect(64, 44, 404, 160, 8, 12, 20);
        txt(100, 108, "SSH LINK OFFLINE", 255, 59, 84, &AA_FACE_16);
        return;
    }
    int cy = 86;
    /* qual CPU: mesmo esquema do nome da GPU, na linha de identidade */
    txt(152, 26, "//", 18, 60, 78, &AA_FACE_11);
    txt(170, 26, "CPU", 60, 90, 110, &AA_FACE_11);
    txt(170 + tw(&AA_FACE_11, "CPU "), 26, d->pccn[0] ? d->pccn : "---",
        d->pccn[0] ? 34 : 60, d->pccn[0] ? 224 : 90, d->pccn[0] ? 255 : 110, &AA_FACE_11);
    /* qual GPU: o nome vem junto do feed (pg=), para o painel nao mostrar so "GPU" */
    if (d->pcgn[0]) {
        txt(64, 42, "GPU", 60, 90, 110, &AA_FACE_11);
        txt(64 + tw(&AA_FACE_11, "GPU "), 42, d->pcgn, 157, 123, 255, &AA_FACE_11);
    } else {
        txt(64, 42, "GPU ---", 60, 90, 110, &AA_FACE_11);
    }
    ringg(96, cy, 30, d->pcc, t, 34, 224, 255);
    snprintf(b, sizeof b, "%d", (int)d->pcc);
    txt_c(96, cy - 9, b, 255, 255, 255, &AA_FACE_NUM_22);
    txt(96 - 12, cy + 12, "CPU", 60, 90, 110, &AA_FACE_11);

    ringg(186, cy, 30, d->pcg, t, 157, 123, 255);
    snprintf(b, sizeof b, "%d", (int)d->pcg);
    txt_c(186, cy - 9, b, 255, 255, 255, &AA_FACE_NUM_22);
    txt(186 - 12, cy + 12, "GPU", 60, 90, 110, &AA_FACE_11);

    int bx = 236;
    snprintf(b, sizeof b, "RAM %d%%", (int)d->pcm);
    txt(bx, 36, b, 200, 233, 245, &AA_FACE_11);
    segbar(bx, 46, 232, 8, d->pcm, 125, 255, 94);
    snprintf(b, sizeof b, "CPU %.0f\xB0" "C  GPU %.0f\xB0" "C", d->pcct, d->pcgt);
    txt(bx, 60, b, d->pcct > 75 ? 255 : 200, d->pcct > 75 ? 59 : 233, d->pcct > 75 ? 84 : 245, &AA_FACE_11);
    snprintf(b, sizeof b, "FAN %d %d %d %d %d", d->pcf[0], d->pcf[1], d->pcf[2], d->pcf[3], d->pcf[4]);
    txt(bx, 72, b, 60, 90, 110, &AA_FACE_11);
    if (d->pcpw > 1)
        snprintf(b, sizeof b, "GPUFAN %d  PWR %.0fW", d->pcgf, d->pcpw);
    else
        snprintf(b, sizeof b, "GPUFAN %s", d->pcgf ? "" : "STOP");
    txt(bx, 84, b, 60, 90, 110, &AA_FACE_11);
    snprintf(b, sizeof b, "LOAD %.2f  UP %dh%02dm", d->pcl, d->pcu / 3600, (d->pcu / 60) % 60);
    txt(bx, 96, b, 200, 233, 245, &AA_FACE_11);

    frame_rect(64, 118, 404, 112, 12, 40, 54);
    if (d->pcn > 1) {
        int pk, vl;
        char pb[24];
        peakval(d->hpc, d->pcn, &pk, &vl);
        (void)pk; (void)vl;
        snprintf(pb, sizeof pb, "%d%%", d->pcn ? d->hpc[d->pcn - 1] : 0);
        txt(64, 121, "CPU", 34, 224, 255, &AA_FACE_11);
        txt(92, 121, pb, 34, 224, 255, &AA_FACE_11);
        peakval(d->hpg, d->pcn, &pk, &vl);
        snprintf(pb, sizeof pb, "%d%%", d->pcn ? d->hpg[d->pcn - 1] : 0);
        txt(190, 121, "GPU", 157, 123, 255, &AA_FACE_11);
        txt(218, 121, pb, 157, 123, 255, &AA_FACE_11);
        peakval(d->hpm, d->pcn, &pk, &vl);
        snprintf(pb, sizeof pb, "%d%%", d->pcn ? d->hpm[d->pcn - 1] : 0);
        txt(316, 121, "RAM", 125, 255, 94, &AA_FACE_11);
        txt(344, 121, pb, 125, 255, 94, &AA_FACE_11);
    } else {
        txt(68, 121, "CPU", 34, 224, 255, &AA_FACE_11);
        txt(100, 121, "GPU", 157, 123, 255, &AA_FACE_11);
        txt(132, 121, "RAM", 125, 255, 94, &AA_FACE_11);
    }
    int mv = 10;
    for (int i = 0; i < d->pcn; i++) {
        if (d->hpc[i] > mv) mv = d->hpc[i];
        if (d->hpg[i] > mv) mv = d->hpg[i];
        if (d->hpm[i] > mv) mv = d->hpm[i];
    }
    mv = mv * 13 / 10;
    spark(68, 134, 396, 88, d->hpc, d->pcn, 34, 224, 255, mv);
    spark(68, 134, 396, 88, d->hpg, d->pcn, 157, 123, 255, mv);
    spark(68, 134, 396, 88, d->hpm, d->pcn, 125, 255, 94, mv);
    cur_label(68, 134, 396, 88, d->hpc, d->pcn, mv, 34, 224, 255);
    cur_label(68, 134, 396, 88, d->hpg, d->pcn, mv, 157, 123, 255);
    cur_label(68, 134, 396, 88, d->hpm, d->pcn, mv, 125, 255, 94);
}

static void tab_dock(struct data *d) {
    char b[64];
    txt(64, 26, "CONTAINERS", 34, 224, 255, &AA_FACE_13);
    snprintf(b, sizeof b, "%d/%d UP", d->dockU, d->dockN);
    txt(380, 26, b, d->dockU == d->dockN ? 125 : 255, d->dockU == d->dockN ? 255 : 59, d->dockU == d->dockN ? 94 : 84, &AA_FACE_11);
    line(64, 36, 470, 36, 18, 60, 78);
    for (int i = 0; i < d->dn && i < 10; i++) {
        int y = 42 + i * 20;
        rect(64, y + 2, 4, 4, d->d[i].up ? 125 : 255, d->d[i].up ? 255 : 59, d->d[i].up ? 94 : 84);
        txt(74, y, d->d[i].name, 200, 233, 245, &AA_FACE_11);
        segbar(190, y, 120, 7, d->d[i].cpu, 34, 224, 255);
        snprintf(b, sizeof b, "%4.1f%%", d->d[i].cpu);
        txt(316, y, b, 60, 90, 110, &AA_FACE_11);
        segbar(370, y, 60, 7, d->d[i].mem, 255, 47, 214);
        snprintf(b, sizeof b, "%4.1f%%", d->d[i].mem);
        txt(436, y, b, 60, 90, 110, &AA_FACE_11);
    }
}

/* 3x5 numerals: three octet digits in an 11 px matrix cell (3+1+3+1+3) */
static const unsigned char DIG35[10][5] = {
    {7,5,5,5,7},{2,6,2,2,7},{7,1,7,4,7},{7,1,7,1,7},{5,5,7,1,1},
    {7,4,7,1,7},{7,4,7,5,7},{7,1,2,2,2},{7,5,7,5,7},{7,5,7,1,7}
};

static void octet3(int x, int y, int v, int r, int g, int b) {
    char s[4];
    snprintf(s, sizeof s, "%03d", v < 0 ? 0 : v > 999 ? 999 : v);
    for (int k = 0; k < 3; k++) {
        int d = s[k] - '0', ox = x + k * 4;
        for (int row = 0; row < 5; row++) {
            unsigned char m = DIG35[d][row];
            if (m & 4) px(ox, y + row, r, g, b);
            if (m & 2) px(ox + 1, y + row, r, g, b);
            if (m & 1) px(ox + 2, y + row, r, g, b);
        }
    }
}

static void tab_net(struct data *d, int t) {
    char b[48];
    txt(64, 26, "NETWORK", 125, 255, 94, &AA_FACE_13);
    snprintf(b, sizeof b, "%d", d->alive);
    txt(64, 37, b, 255, 255, 255, &AA_FACE_NUM_40);
    snprintf(b, sizeof b, "/%d HOSTS", d->total);
    txt(64, 73, b, 60, 90, 110, &AA_FACE_11);
    txt(64, 89, "SCAN 192.168.5.0/24", 60, 90, 110, &AA_FACE_11);
    txt(64, 103, "LINK OMASERVER OK", d->online ? 125 : 255, d->online ? 255 : 59, d->online ? 94 : 84, &AA_FACE_11);
    /* every host the server ranked, two columns: ".129 0.2ms" */
    for (int i = 0; i < d->vn && i < 20; i++) {
        int cx = 64 + (i / 10) * 72, cy = 119 + (i % 10) * 12;
        snprintf(b, sizeof b, ".%d", d->v[i].ip);
        txt(cx, cy, b, 90, 110, 130, &AA_FACE_11);
        if (d->v[i].rtt >= 10) snprintf(b, sizeof b, "%.0fms", d->v[i].rtt);
        else snprintf(b, sizeof b, "%.1fms", d->v[i].rtt);
        txt(cx + 32, cy, b, d->v[i].rtt < 5 ? 125 : d->v[i].rtt < 25 ? 255 : 255,
             d->v[i].rtt < 5 ? 255 : d->v[i].rtt < 25 ? 226 : 59,
             d->v[i].rtt < 5 ? 94 : d->v[i].rtt < 25 ? 61 : 84, &AA_FACE_11);
    }

    int mx = 250, my = 44;
    for (int i = 1; i < 255; i++) {
        int x = mx + (i % 16) * 13, y = my + (i / 16) * 12;
        if (y > 232) break;
        if (matrix_alive[i]) {
            int pulse = (t / 6 + i) % 8 < 2 ? 255 : 170;
            fill_rrect(x, y, 11, 10, 2.5, 24, pulse, pulse);
            octet3(x, y + 3, i, 3, 22, 30);   /* which address, without counting columns */
        } else
            fill_rrect(x, y, 11, 10, 2.5, 6, 14, 22);
    }
    int col = (t / 20) % 16;
    for (int row = 0; row < 16; row++) {
        int x = mx + col * 13, y = my + row * 12;
        for (int i = 0; i < 11; i++) px(x + i, y, 0, 28, 36);
    }
}

static void cam_tile(int i, int *x, int *y, int *w, int *h) {
    *w = 201; *h = 91;
    *x = 62 + (i % 2) * 206;
    *y = 40 + (i / 2) * 96;
}

static void blit_fit(unsigned char *src, int sw, int sh, int dx, int dy, int dw, int dh) {
    int tw = dw, th = dw * sh / sw;
    if (th > dh) { th = dh; tw = dh * sw / sh; }
    int ox = dx + (dw - tw) / 2, oy = dy + (dh - th) / 2;
    for (int y = 0; y < th; y++) {
        int sy = y * sh / th;
        for (int x = 0; x < tw; x++) {
            int sx = x * sw / tw;
            unsigned char *p = src + (sy * sw + sx) * 3;
            px(ox + x, oy + y, p[0], p[1], p[2]);
        }
    }
}

static void loading_box(int x, int y, int w, int h, int t) {
    rect(x + 2, y + 2, w - 4, h - 4, 6, 10, 16);
    for (int yy = 8; yy < h - 6; yy += 8)
        for (int xx = 6; xx < w - 6; xx++) px(x + xx, y + yy, 16, 24, 32);
    int n = 1 + (t / 15) % 3;
    char msg[16] = "LOADING";
    int l = 7 + n;
    for (int i = 7; i < l; i++) msg[i] = '.';
    msg[l] = 0;
    txt(x + w / 2 - (l * 7) / 2, y + h / 2 - 3, msg, 34, 224, 255, &AA_FACE_11);
}

static void no_signal(int x, int y, int w, int h) {
    rect(x + 2, y + 2, w - 4, h - 4, 6, 10, 16);
    for (int yy = 8; yy < h - 6; yy += 8)
        for (int xx = 6; xx < w - 6; xx++) px(x + xx, y + yy, 16, 24, 32);
    txt(x + w / 2 - 27, y + h / 2 - 3, "NO SIGNAL", 255, 59, 84, &AA_FACE_11);
}

static void tab_cam(int t) {
    char b[48];
    if (cam_zoom < 0) {
        txt(64, 26, "CAMERAS 2X2", 255, 47, 214, &AA_FACE_13);
        txt(392, 26, "TAP TO ZOOM", 60, 90, 110, &AA_FACE_11);
        for (int i = 0; i < 4; i++) {
            int x, y, w, h;
            cam_tile(i, &x, &y, &w, &h);
            rect(x, y, w, h, 4, 7, 12);
            frame_rect(x, y, w, h, 18, 60, 78);
            if (camok[i]) blit_fit(cambuf[i], CW, CH, x, y, w, h);
            else if (cam_enter_sec && time(NULL) - cam_enter_sec < 15 && i < 2) loading_box(x, y, w, h, t);
            else no_signal(x, y, w, h);
            rect(x, y, w, 12, 5, 8, 14);
            txt(x + 4, y + 3, CAMSRC[i], 255, 47, 214, &AA_FACE_11);
            txt(x + w - 26, y + 3, camok[i] ? "ON" : "OFF",
                 camok[i] ? 125 : 255, camok[i] ? 255 : 59, camok[i] ? 94 : 84, &AA_FACE_11);
        }
    } else {
        int i = cam_zoom;
        int x = 62, y = 40, w = 412, h = 192;
        rect(x, y, w, h, 4, 7, 12);
        frame_rect(x, y, w, h, 34, 224, 255);
        if (camok[i]) blit_fit(cambuf[i], CW, CH, x, y, w, h);
        else if (cam_enter_sec && time(NULL) - cam_enter_sec < 15) loading_box(x, y, w, h, t);
        else no_signal(x, y, w, h);
        rect(x, y, w, 14, 5, 8, 14);
        snprintf(b, sizeof b, "%s  LIVE", CAMSRC[i]);
        txt(x + 4, y + 4, b, 255, 47, 214, &AA_FACE_11);
        txt(x + w - 70, y + 4, "TAP=CLOSE", 34, 224, 255, &AA_FACE_11);
    }
}

static void render(struct data *d, struct wx *w, int cur, int t) {
    char b[80];
    bg(t);
    draw_header(d, t);
    draw_rail(cur, t);
    rect(58, 22, 420, 222, 6, 9, 16);          /* content backdrop */
    line(58, 22, 478, 22, 16, 48, 64);

    switch (cur) {
    case 0: tab_sys(d, t); break;
    case 1: tab_pc(d, t); break;
    case 2: tab_dock(d); break;
    case 3: tab_net(d, t); break;
    case 4: tab_cam(t); break;
    case 5: tab_wx(w, t); break;
    }

    /* state line: what the panel is doing (was a decorative marquee) */
    char sb[96];
    snprintf(sb, sizeof sb, "%s   |   %d HOSTS   |   DOCK %d/%d   |   %s",
             d->online ? "LINK OK" : "LINK LOST", d->alive, d->dockU, d->dockN, d->clock);
    txt(64, 257, sb, d->online ? 90 : 255, d->online ? 130 : 59, d->online ? 150 : 84, &AA_FACE_11);
    fill_circle(57, 261, 2.5, d->online ? 125 : 255, d->online ? 255 : 59, d->online ? 94 : 84);

    if (!d->online) {
        snprintf(b, sizeof b, "API OFFLINE");
        rect(W - 90, 4, 86, 13, 40, 0, 12);
        txt(W - 86, 7, b, 255, 59, 84, &AA_FACE_11);
    }
    rect(0, H - 1, W, 1, 34, 224, 255);
}

int main(int argc, char **argv) {
    int cur = 0, once = 0, fixed = 0, nofb = 0, bench = 0;
    const char *ppmout = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--rot")) rot180 = 1;
        else if (!strcmp(argv[i], "--once")) once = 1;
        else if (!strcmp(argv[i], "--tab") && i + 1 < argc) { cur = atoi(argv[++i]) % NTAB; fixed = 1; }
        else if (!strcmp(argv[i], "--zoom") && i + 1 < argc) cam_zoom = atoi(argv[++i]) % 4;
        else if (!strcmp(argv[i], "--nofb")) nofb = 1;                 /* render without /dev/fb0 */
        else if (!strcmp(argv[i], "--ppm") && i + 1 < argc) ppmout = argv[++i];
        else if (!strcmp(argv[i], "--bench") && i + 1 < argc) bench = atoi(argv[++i]);
    }
    srand((unsigned)time(NULL));
    if (bench) nofb = 1;
    if (!nofb && !ppmout) {
        int fd = open("/dev/fb0", O_RDWR);
        if (fd < 0) { perror("fb0"); return 1; }
        /* one writer only: a second copy (test build, stale binary) must not
           paint over the live interface - that is exactly how the panel ended
           up alternating between two UIs */
        if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
            fprintf(stderr, "fbdash: /dev/fb0 is owned by another instance, exiting\n");
            return 2;
        }
        fb = mmap(NULL, FB_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        if (fb == MAP_FAILED) { perror("mmap"); return 1; }
    }

    for (int i = 0; i < 4; i++) cambuf[i] = malloc(CW * CH * 3);

    touch_open();
    cal_load();
    if (!nofb && !ppmout && !once && !cal_ok) run_calibration();
    struct data d;
    struct wx w;
    memset(&d, 0, sizeof d);
    memset(&w, 0, sizeof w);
    collect(&d);
    seed_history(&d);
    if (ppmout) fetch_wx(&w);       /* offscreen frame dump: fetch before the first render */

    struct timespec next;
    clock_gettime(CLOCK_MONOTONIC, &next);
    int last_cam = -1000, prev_cur = cur, last_wx = 0;
    for (int t = 0;; t++) {
        if (t % 120 == 0) collect(&d);
        if (t == 2 || t - last_wx > 60 * 600) { fetch_wx(&w); last_wx = t; }
        else if (cur == 5 && !w.have && t - last_wx > 60 * 5) { fetch_wx(&w); last_wx = t; }
        touch_poll();
        if (tap) {
            int tx, ty;
            touch_map(&tx, &ty);
            if (tx < 58 && ty >= 20) {
                int idx = (ty - RAILY) / RAILP;
                if (idx < 0) idx = 0;
                if (idx >= NTAB) idx = NTAB - 1;
                cur = idx;
                if (cur != 4) cam_zoom = -1;
            } else if (cur == 4) {
                if (cam_zoom >= 0) {
                    cam_zoom = -1;
                } else {
                    for (int i = 0; i < 4; i++) {
                        int x, y, w, h;
                        cam_tile(i, &x, &y, &w, &h);
                        if (tx >= x && tx < x + w && ty >= y && ty < y + h) {
                            cam_zoom = i;
                            break;
                        }
                    }
                }
            }
            {
                FILE *dbg = fopen("/tmp/touchdbg", "a");
                if (dbg) { fprintf(dbg, "%ld raw %d %d map %d %d idx %d\n",
                                   (long)time(NULL), rawx, rawy, tx, ty,
                                   (ty >= 20 && tx < 58) ? (ty - 20) / 56 : -1); fclose(dbg); }
            }
        }
        if (cur == 4 && cam_enter_sec == 0) cam_enter_sec = time(NULL);
        if (cur != 4) cam_enter_sec = 0;
        struct timespec tb0;
        if (bench && t == 0) clock_gettime(CLOCK_MONOTONIC, &tb0);
        render(&d, &w, cur, t);
        if (bench && t == bench - 1) {
            struct timespec tb1;
            clock_gettime(CLOCK_MONOTONIC, &tb1);
            double ms = ((tb1.tv_sec - tb0.tv_sec) * 1000.0 + (tb1.tv_nsec - tb0.tv_nsec) / 1e6) / bench;
            if (ppmout) {                       /* dump the last (cached-path) frame */
                FILE *pf = fopen(ppmout, "wb");
                if (pf) {
                    fprintf(pf, "P6\n%d %d\n255\n", W, H);
                    unsigned char *rgb = malloc(FB_SIZE);
                    if (rgb) {
                        for (int i = 0; i < FB_SIZE; i += 3) {
                            rgb[i] = drawbuf[i + 2];
                            rgb[i + 1] = drawbuf[i + 1];
                            rgb[i + 2] = drawbuf[i];
                        }
                        fwrite(rgb, 1, FB_SIZE, pf);
                        free(rgb);
                    }
                    fclose(pf);
                }
            }
            printf("%d frames: %.2f ms/frame (%.0f fps), tab %d\n", bench, ms, 1000.0 / ms, cur);
            return 0;
        }
        if (cur == 4 && t - last_cam > 240) { launch_cams(); last_cam = t; }
        if (cur == 4 && t % 60 == 0) load_cams();
        if (ppmout) {
            FILE *pf = fopen(ppmout, "wb");
            if (pf) {
                fprintf(pf, "P6\n%d %d\n255\n", W, H);
                /* drawbuf is BGR (framebuffer order); PPM wants RGB */
                unsigned char *rgb = malloc(FB_SIZE);
                if (rgb) {
                    for (int i = 0; i < FB_SIZE; i += 3) {
                        rgb[i] = drawbuf[i + 2];
                        rgb[i + 1] = drawbuf[i + 1];
                        rgb[i + 2] = drawbuf[i];
                    }
                    fwrite(rgb, 1, FB_SIZE, pf);
                    free(rgb);
                }
                fclose(pf);
            }
            return 0;
        }
        if (fb) {
            memcpy(fb, drawbuf, FB_SIZE);
            msync(fb, FB_SIZE, MS_ASYNC);
        }
        if (once) { usleep(300000); break; }

        if (bench) continue;                 /* no pacing: measure raw render cost */
        if (cur != prev_cur) { clock_gettime(CLOCK_MONOTONIC, &next); prev_cur = cur; }
        long step = (cur == 4) ? 66666667L : 16666667L;   /* camera tab: 15 fps, others 60 fps */
        next.tv_nsec += step;
        if (next.tv_nsec >= 1000000000L) { next.tv_sec++; next.tv_nsec -= 1000000000L; }
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
    }
    return 0;
}
