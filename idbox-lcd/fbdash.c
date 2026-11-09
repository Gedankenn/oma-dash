/* fbdash v2 - cyberpunk status screen for Control iD iDBoxV2 (480x272 RGB24) */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <math.h>
#include <sys/mman.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define W 480
#define H 272
#define STRIDE 1440
#define FB_SIZE (STRIDE * H)

static unsigned char *fb;
static int rot180 = 0;

static inline void px(int x, int y, int r, int g, int b) {
    if (x < 0 || y < 0 || x >= W || y >= H) return;
    if (rot180) { x = W - 1 - x; y = H - 1 - y; }
    unsigned char *p = fb + y * STRIDE + x * 3;
    p[0] = b; p[1] = g; p[2] = r;
}
static void rect(int x, int y, int w, int h, int r, int g, int b) {
    if (w <= 0 || h <= 0) return;
    for (int j = 0; j < h; j++)
        for (int i = 0; i < w; i++) px(x + i, y + j, r, g, b);
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
    rect(x, y, w, 1, r, g, b); rect(x, y + h - 1, w, 1, r, g, b);
    rect(x, y, 1, h, r, g, b); rect(x + w - 1, y, 1, h, r, g, b);
}
static void corners(int x, int y, int w, int h, int l, int r, int g, int b) {
    rect(x, y, l, 2, r, g, b); rect(x, y, 2, l, r, g, b);
    rect(x + w - l, y, l, 2, r, g, b); rect(x + w - 2, y, 2, l, r, g, b);
    rect(x, y + h - 2, l, 2, r, g, b); rect(x, y + h - l, 2, l, r, g, b);
    rect(x + w - l, y + h - 2, l, 2, r, g, b); rect(x + w - 2, y + h - l, 2, l, r, g, b);
}

/* ---------------- 5x7 font ---------------- */
static const char *CHARS = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:./-+<>";
static const char *GLYPHS[][7] = {
{"00000","00000","00000","00000","00000","00000","00000"},
{"01110","10001","10001","11111","10001","10001","10001"},
{"11110","10001","10001","11110","10001","10001","11110"},
{"01111","10000","10000","10000","10000","10000","01111"},
{"11110","10001","10001","10001","10001","10001","11110"},
{"11111","10000","10000","11110","10000","10000","11111"},
{"11111","10000","10000","11110","10000","10000","10000"},
{"01111","10000","10000","10111","10001","10001","01111"},
{"10001","10001","10001","11111","10001","10001","10001"},
{"11111","00100","00100","00100","00100","00100","11111"},
{"00111","00010","00010","00010","10010","10010","01100"},
{"10001","10010","10100","11000","10100","10010","10001"},
{"10000","10000","10000","10000","10000","10000","11111"},
{"10001","11011","10101","10101","10001","10001","10001"},
{"10001","11001","10101","10011","10001","10001","10001"},
{"01110","10001","10001","10001","10001","10001","01110"},
{"11110","10001","10001","11110","10000","10000","10000"},
{"01110","10001","10001","10001","10101","10010","01101"},
{"11110","10001","10001","11110","10100","10010","10001"},
{"01111","10000","10000","01110","00001","00001","11110"},
{"11111","00100","00100","00100","00100","00100","00100"},
{"10001","10001","10001","10001","10001","10001","01110"},
{"10001","10001","10001","10001","10001","01010","00100"},
{"10001","10001","10001","10101","10101","10101","01010"},
{"10001","10001","01010","00100","01010","10001","10001"},
{"10001","10001","01010","00100","00100","00100","00100"},
{"11111","00001","00010","00100","01000","10000","11111"},
{"01110","10001","10011","10101","11001","10001","01110"},
{"00100","01100","00100","00100","00100","00100","01110"},
{"01110","10001","00001","00010","00100","01000","11111"},
{"11111","00010","00100","00010","00001","10001","01110"},
{"00010","00110","01010","10010","11111","00010","00010"},
{"11111","10000","11110","00001","00001","10001","01110"},
{"00110","01000","10000","11110","10001","10001","01110"},
{"11111","00001","00010","00100","01000","01000","01000"},
{"01110","10001","10001","01110","10001","10001","01110"},
{"01110","10001","10001","01111","00001","00010","01100"},
{"00000","00100","00000","00000","00100","00000","00000"},
{"00000","00000","00000","00000","00000","00110","00110"},
{"11001","11010","00100","00100","01000","01011","10011"},
{"00001","00010","00010","00100","01000","01000","10000"},
{"00000","00000","00000","11111","00000","00000","00000"},
{"00000","00100","00100","11111","00100","00100","00000"},
{"00010","00100","01000","10000","01000","00100","00010"},
{"01000","00100","00010","00001","00010","00100","01000"},
};
static int gidx(char c) {
    if (c >= 'a' && c <= 'z') c -= 32;
    const char *p = strchr(CHARS, c);
    return p ? (int)(p - CHARS) : 0;
}
static void text(int x, int y, const char *s, int r, int g, int b, int sc) {
    for (; *s; s++, x += 6 * sc) {
        const char **gl = GLYPHS[gidx(*s)];
        for (int row = 0; row < 7; row++)
            for (int col = 0; col < 5; col++)
                if (gl[row][col] == '1') rect(x + col * sc, y + row * sc, sc, sc, r, g, b);
    }
}
static void text_glow(int x, int y, const char *s, int r, int g, int b, int sc) {
    text(x + 1, y, s, r / 3, g / 3, b / 3, sc);
    text(x, y, s, r, g, b, sc);
}

/* ---------------- data ---------------- */
struct data {
    double cpu, mem, load1, load2, load3, temp, gpu, uptime;
    int dock, dockdown, svcdown;
    int alive, rtt, devcpu, devmem, devtemp, devuptime;
    long long ts;
    char clock[16];
};
static unsigned char hcpu[96], hmem[96];
static int hn = 0;
static unsigned char alive_map[255], seen_map[255];
static int last_alive = 0;

static char *fetch(const char *url, int timeout) {
    char cmd[256];
    snprintf(cmd, sizeof cmd, "wget -q -T %d -O - '%s' 2>/dev/null", timeout, url);
    FILE *f = popen(cmd, "r");
    if (!f) return NULL;
    static char buf[65536];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    pclose(f);
    buf[n] = 0;
    return n ? buf : NULL;
}
static double jnum(const char *s, const char *key) {
    const char *p = strstr(s, key);
    if (!p) return -1;
    p += strlen(key);
    while (*p && (*p < '0' || *p > '9') && *p != '-') p++;
    return atof(p);
}
static int jcount(const char *s, const char *key) {
    int n = 0; const char *p = s;
    while ((p = strstr(p, key))) { n++; p += strlen(key); }
    return n;
}
static void read_local(struct data *d) {
    static long pt = 0, pi = 0;
    FILE *f = fopen("/proc/stat", "r");
    if (f) {
        char line[256]; long u, n, s, i, w, ir, si;
        if (fgets(line, sizeof line, f) && sscanf(line, "cpu %ld %ld %ld %ld %ld %ld %ld", &u, &n, &s, &i, &w, &ir, &si) == 7) {
            long tot = u + n + s + i + w + ir + si, idl = i + w;
            if (pt) { long dt = tot - pt, di = idl - pi; if (dt > 0) d->devcpu = (int)(100 * (dt - di) / dt); }
            pt = tot; pi = idl;
        }
        fclose(f);
    }
    f = fopen("/proc/meminfo", "r");
    if (f) {
        char line[128]; long mt = 0, ma = 0;
        while (fgets(line, sizeof line, f)) {
            if (!strncmp(line, "MemTotal:", 9)) mt = atol(line + 9);
            if (!strncmp(line, "MemAvailable:", 13)) ma = atol(line + 13);
        }
        fclose(f);
        if (mt) d->devmem = (int)(100 * (mt - ma) / mt);
    }
    f = fopen("/sys/class/thermal/thermal_zone0/temp", "r");
    if (f) { int t = 0; if (fscanf(f, "%d", &t) == 1) d->devtemp = t / 1000; fclose(f); }
    f = fopen("/proc/uptime", "r");
    if (f) { double up = 0; if (fscanf(f, "%lf", &up) == 1) d->devuptime = (int)up; fclose(f); }
}
static void collect(struct data *d) {
    memset(d, 0, sizeof *d);
    static char stats[32768], net[32768];
    char *s = fetch("http://192.168.5.245:1985/stats", 2);
    if (s) strncpy(stats, s, sizeof stats - 1), stats[sizeof stats - 1] = 0, d->rtt = 0;
    else d->rtt = -1;

    if (stats[0]) {
        d->cpu = jnum(stats, "\"cpu\":");
        d->mem = jnum(stats, "\"used\":");
        double total = jnum(stats, "\"total\":");
        if (total > 0) d->mem = 100.0 * d->mem / total;
        const char *lp = strstr(stats, "\"load\":");
        if (lp) { const char *br = strchr(lp, '['); if (br) sscanf(br + 1, "%lf,%lf,%lf", &d->load1, &d->load2, &d->load3); }
        d->temp = jnum(stats, "\"c\":");
        if (d->temp > 1000) d->temp /= 1000.0;
        d->gpu = jnum(stats, "\"gpu\":");
        d->uptime = jnum(stats, "\"uptime\":");
        d->dock = jcount(stats, "\"status\":");
        d->dockdown = jcount(stats, "\"up\":false");
        d->svcdown = jcount(stats, "\"ok\":false");
        d->ts = (long long)jnum(stats, "\"ts\":");
        if (d->ts > 1600000000LL) {
            time_t st = (time_t)(d->ts - 3 * 3600);
            struct tm *gt = gmtime(&st);
            strftime(d->clock, sizeof d->clock, "%H:%M:%S", gt);
        }
    }
    static int ntick = 0;
    if (ntick++ % 6 == 0) {
        char *n = fetch("http://192.168.5.245:1985/network", 3);
        if (n) {
            strncpy(net, n, sizeof net - 1); net[sizeof net - 1] = 0;
            memset(alive_map, 0, sizeof alive_map);
            memset(seen_map, 0, sizeof seen_map);
            const char *p = net;
            while ((p = strstr(p, "192.168.5."))) {
                int ip = atoi(p + 10);
                if (ip > 0 && ip < 255) {
                    const char *q = strstr(p, "\"alive\":");
                    seen_map[ip] = 1;
                    if (q && q - p < 1500) {
                        const char *v = q + 8;
                        while (*v == ' ') v++;
                        if (!strncmp(v, "true", 4)) alive_map[ip] = 1;
                    }
                }
                p += 10;
            }
            last_alive = (int)jnum(net, "\"alive\":");
            d->alive = last_alive;
            {
                FILE *dbg = fopen("/tmp/fbdbg", "w");
                if (dbg) { int seen = 0, al = 0; for (int i = 0; i < 255; i++) { seen += seen_map[i] ? 1 : 0; al += alive_map[i] ? 1 : 0; }
                    fprintf(dbg, "netlen=%zu alive=%d seen=%d al=%d\n", strlen(net), d->alive, seen, al); fclose(dbg); }
            }
        }
    }
    d->alive = last_alive;
    read_local(d);
    if (d->cpu < 0) d->cpu = 0;
    if (d->cpu > 100) d->cpu = 100;
    if (d->mem < 0) d->mem = 0;
    if (d->mem > 100) d->mem = 100;

    if (hn < 96) {
        hcpu[hn] = (unsigned char)d->cpu;
        hmem[hn] = (unsigned char)d->mem;
        hn++;
    } else {
        memmove(hcpu, hcpu + 1, 95);
        memmove(hmem, hmem + 1, 95);
        hcpu[95] = (unsigned char)d->cpu;
        hmem[95] = (unsigned char)d->mem;
    }
}

/* ---------------- graphics ---------------- */
static void bg(int t) {
    rect(0, 0, W, H, 4, 6, 11);
    int horizon = 118;
    /* perspective floor */
    for (int i = 0; i < 12; i++) {
        double k = i + (t % 20) / 20.0;
        int y = horizon + (int)(k * k * 1.6);
        if (y >= H) break;
        int a = 8 + i * 2;
        for (int x = 0; x < W; x++) px(x, y, 4, a, a * 1.7);
    }
    for (int i = -12; i <= 12; i++) {
        int xb = W / 2 + i * 46;
        line(W / 2, horizon, xb, H - 1, 3, 14, 24);
    }
    line(0, horizon, W, horizon, 5, 20, 32);
    /* moving scanline */
    int sy = (t * 5) % (H + 40) - 20;
    for (int y = sy - 2; y <= sy + 2; y++)
        for (int x = 0; x < W; x++) px(x, y, 6, 40, 55);
    /* glitch bars */
    if (t % 47 < 2) {
        for (int k = 0; k < 3; k++) {
            int y = rand() % H, h = 1 + rand() % 3, w = 40 + rand() % 160, x = rand() % (W - w);
            rect(x, y, w, h, rand() % 2 ? 60 : 40, 0, 30);
        }
    }
}
static void segbar(int x, int y, int w, int h, double pct, int r, int g, int b) {
    int blocks = w / 5;
    int on = (int)(blocks * pct / 100.0);
    for (int i = 0; i < blocks; i++) {
        int bx = x + i * 5;
        if (i < on) rect(bx, y, 4, h, r, g, b);
        else rect(bx, y, 4, h, 10, 18, 26);
    }
}
static void ring(int cx, int cy, int rad, double pct, int r, int g, int b, int t) {
    for (int a = 0; a < 360; a += 3) {
        double rad_a = a * M_PI / 180.0;
        int x = cx + (int)(cos(rad_a) * rad), y = cy + (int)(sin(rad_a) * rad);
        int on = a <= pct * 3.6;
        int pulse = (t % 40 < 20) ? 0 : 10;
        if (on) rect(x, y, 2, 2, r, g, (b > 200 ? 255 : b));
        else rect(x, y, 2, 2, 12, 22 + pulse / 2, 30 + pulse / 2);
    }
}
static void spark(int x, int y, int w, int h, unsigned char *hist, int n, int r, int g, int b) {
    if (n < 2) return;
    int px0 = x, py0 = y + h - (hist[0] * h / 100);
    for (int i = 1; i < n; i++) {
        int px1 = x + i * w / (n - 1), py1 = y + h - (hist[i] * h / 100);
        line(px0, py0, px1, py1, r, g, b);
        px0 = px1; py0 = py1;
    }
    rect(px0 - 1, py0 - 1, 3, 3, 255, 255, 255);
}
static void radar(int cx, int cy, int rad, int t, int hosts) {
    for (int a = 0; a < 360; a += 6) {
        double ra = a * M_PI / 180.0;
        px(cx + (int)(cos(ra) * rad), cy + (int)(sin(ra) * rad), 10, 40, 50);
    }
    line(cx - rad, cy, cx + rad, cy, 6, 22, 30);
    line(cx, cy - rad, cx, cy + rad, 6, 22, 30);
    double ang = (t * 6 % 360) * M_PI / 180.0;
    line(cx, cy, cx + (int)(cos(ang) * rad), cy + (int)(sin(ang) * rad), 34, 224, 255);
    for (int i = 0; i < hosts && i < 9; i++) {
        double ra = (i * 47 % 360) * M_PI / 180.0, rr = 8 + (i * 13) % (rad - 6);
        rect(cx + (int)(cos(ra) * rr), cy + (int)(sin(ra) * rr), 2, 2, 125, 255, 94);
    }
}
static void matrix(int x, int y, int t) {
    for (int i = 1; i < 255; i++) {
        int mx = x + (i % 16) * 5, my = y + (i / 16) * 5;
        if (i / 16 > 15) break;
        if (alive_map[i]) {
            int pulse = (t / 6 + i) % 8 < 2 ? 255 : 160;
            rect(mx, my, 4, 4, 40, pulse, pulse);
        } else if (seen_map[i]) {
            rect(mx, my, 4, 4, 10, 26, 34);
        } else {
            rect(mx, my, 4, 4, 2, 8, 12);
        }
    }
}

static void render(struct data *d, int t) {
    char buf[64];
    bg(t);

    /* header */
    rect(0, 0, W, 20, 5, 8, 14);
    int jit = (t % 53 == 0) ? (rand() % 3 - 1) : 0;
    text(jit, 5, "OMA", 255, 47, 214, 2);
    text(38 + jit, 5, "//", 34, 224, 255, 2);
    text(62 + jit, 5, "DASH", 255, 255, 255, 2);
    text_glow(W - 58, 6, d->clock, 34, 224, 255, 1);
    if (t % 20 < 10) rect(W - 8, 8, 3, 3, 125, 255, 94);
    line(0, 20, W, 20, 34, 224, 255);

    /* left panel */
    frame_rect(4, 24, 296, 150, 18, 60, 78);
    corners(4, 24, 296, 150, 10, 34, 224, 255);
    text(8, 27, "OMASERVER", 255, 47, 214, 1);
    text(80, 27, "/ 192.168.5.245", 60, 90, 110, 1);

    int cx = 40, cy = 82, rad = 30;
    ring(cx, cy, rad, d->cpu, 34, 224, 255, t);
    snprintf(buf, sizeof buf, "%d", (int)d->cpu);
    text(cx - (strlen(buf) * 6) / 2, cy - 8, buf, 255, 255, 255, 2);
    text(cx - 9, cy + 8, "CPU%", 60, 90, 110, 1);

    /* sparkline area */
    frame_rect(78, 42, 216, 64, 12, 40, 54);
    spark(80, 44, 212, 60, hcpu, hn, 34, 224, 255);
    spark(80, 44, 212, 60, hmem, hn, 255, 47, 214);

    snprintf(buf, sizeof buf, "MEM %3d%%", (int)d->mem);
    text(78, 112, buf, 200, 233, 245, 1);
    segbar(78, 122, 216, 7, d->mem, 255, 47, 214);

    snprintf(buf, sizeof buf, "LOAD %.2f %.2f %.2f", d->load1, d->load2, d->load3);
    text(8, 134, buf, 125, 255, 94, 1);
    snprintf(buf, sizeof buf, "TEMP %.0fC GPU %.0f%% UP %dd", d->temp, d->gpu, (int)(d->uptime / 86400));
    text(8, 146, buf, d->temp > 80 ? 255 : 200, d->temp > 80 ? 59 : 233, d->temp > 80 ? 84 : 245, 1);
    snprintf(buf, sizeof buf, "DOCKER %d/%d  SVC %s", d->dock - d->dockdown, d->dock, d->svcdown ? "WARN" : "OK");
    text(8, 158, buf, d->dockdown || d->svcdown ? 255 : 125, d->dockdown || d->svcdown ? 59 : 255, d->dockdown || d->svcdown ? 84 : 94, 1);

    /* right panel */
    frame_rect(304, 24, 172, 150, 58, 18, 60);
    corners(304, 24, 172, 150, 10, 255, 47, 214);
    text(308, 27, "IDBOX V2", 255, 47, 214, 1);
    text(308, 40, "CPU", 60, 90, 110, 1);
    segbar(336, 40, 132, 7, d->devcpu, 34, 224, 255);
    text(308, 52, "MEM", 60, 90, 110, 1);
    segbar(336, 52, 132, 7, d->devmem, 255, 47, 214);
    snprintf(buf, sizeof buf, "TEMP %dC", d->devtemp);
    text(308, 64, buf, d->devtemp > 85 ? 255 : 200, d->devtemp > 85 ? 59 : 233, d->devtemp > 85 ? 84 : 245, 1);
    snprintf(buf, sizeof buf, "UP %dh%02dm", d->devuptime / 3600, (d->devuptime / 60) % 60);
    text(308, 76, buf, 200, 233, 245, 1);
    snprintf(buf, sizeof buf, "API %s", d->rtt < 0 ? "LOST" : "OK");
    text(308, 88, buf, d->rtt < 0 ? 255 : 125, d->rtt < 0 ? 59 : 255, d->rtt < 0 ? 84 : 94, 1);
    radar(390, 128, 30, t, d->alive);
    text(360, 162, "SCAN", 60, 90, 110, 1);

    /* bottom */
    frame_rect(4, 178, 472, 92, 18, 60, 78);
    corners(4, 178, 472, 92, 10, 125, 255, 94);
    text(8, 181, "NETWORK MATRIX", 125, 255, 94, 1);
    matrix(8, 194, t);
    /* trailing scan column on matrix */
    int col = (t / 2) % 16;
    for (int row = 0; row < 16; row++) {
        int mx = 8 + col * 5, my = 194 + row * 5;
        for (int i = 0; i < 4; i++) px(mx + i, my, 0, 30, 40);
    }
    snprintf(buf, sizeof buf, "HOSTS %d/254", d->alive);
    text(100, 196, buf, 200, 233, 245, 1);
    snprintf(buf, sizeof buf, "API %s", d->rtt < 0 ? "OFFLINE" : "ONLINE");
    text(100, 208, buf, d->rtt < 0 ? 255 : 34, d->rtt < 0 ? 59 : 224, d->rtt < 0 ? 84 : 255, 1);
    text(100, 220, "UPLINK 192.168.5.245:1985", 60, 90, 110, 1);
    text(100, 232, "SOC ALLWINNER H3 4X A7", 60, 90, 110, 1);
    snprintf(buf, sizeof buf, "FW 20260529  DOCKER %d", d->dock);
    text(100, 244, buf, 60, 90, 110, 1);

    /* ticker */
    static const char *tick = "NEURAL LINK ESTABLISHED /// IDBOX V2 APPLIANCE /// OMASERVER TELEMETRY /// ";
    int tw = strlen(tick) * 6;
    int off = (t * 3) % tw;
    for (int i = 0; i < 2; i++) {
        int sx = 8 + i * tw - off;
        for (const char *p = tick; *p; p++, sx += 6) {
            if (sx < 8 || sx > 470) continue;
            text(sx, 262, (char[]){*p, 0}, 34, 224, 255, 1);
        }
    }
    rect(0, H - 1, W, 1, 34, 224, 255);
}

int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "--rot")) rot180 = 1;
    srand((unsigned)time(NULL));
    int fd = open("/dev/fb0", O_RDWR);
    if (fd < 0) { perror("fb0"); return 1; }
    fb = mmap(NULL, FB_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (fb == MAP_FAILED) { perror("mmap"); return 1; }

    struct data d;
    memset(&d, 0, sizeof d);
    collect(&d);
    for (int t = 0;; t++) {
        if (t % 12 == 0) collect(&d);
        render(&d, t);
        msync(fb, FB_SIZE, MS_ASYNC);
        usleep(220000);
    }
    return 0;
}
