/*
 * fusion_glow — a soft blue glow fading in from the edges of Fusion's main window,
 * shown while the Browser's suspend ("peek") mode is on. Fusion's own red
 * "being controlled" border is the model: say the mode is on, never get in the way.
 *
 *   fusion_glow [--app NAME] [pid]      NAME defaults to Fusion; bin/psmatte passes Photoshop
 *
 * One borderless, click-through, clear, shadowless window at the status level —
 * above normal windows and Fusion's companion windows (floating), so it is never a
 * layer-0 window win_list / slots can see, and
 * yabai is told to leave it alone (config/yabai/yabairc). Every POLL_S it finds
 * Fusion's main window (the largest layer-0 on-screen window owned by the pid given,
 * or with no pid by any process whose name contains "Fusion" — the name flips, see
 * scene_restore.m) and follows its bounds. Hidden while Fusion's window isn't on
 * screen or no Fusion-named app (Fusion itself, or one of its companion windows) is
 * frontmost, so it never floats over an unrelated app. Exits by itself when the pid
 * dies. Started / SIGTERMed by the Browser add-in (AddIns/Browser/Browser.py, _glow).
 *
 * Build: bin/build.sh fusion_glow
 */
#import <Cocoa/Cocoa.h>
#include <signal.h>

static const NSTimeInterval POLL_S = 0.15;
// ── The look ──────────────────────────────────────────────────────────────────
// Every edge fades identically, and a corner is never brighter than the edge: strength
// comes from the CLOSENESS to the two nearest edges, c = (cx^P + cy^P)^(1/P), capped
// at REACH (= right at an edge). P → ∞ is the plain nearest-edge distance, whose mitred
// corners show a hard diagonal crease; P = CORNER_P rounds that crease off without
// rounding the corner itself much. (A blurred inset shadow SUMS the two edges at a
// corner, which is what rounded it right off and lit it up.)
//
// Colour: a light blue, as ordinary translucency (out = a·C + (1 − a)·under). An
// additive blend isn't possible across windows: a pixel carrying colour with zero alpha
// (the premultiplied trick) is clamped to nothing by the window server (tried
// 2026-09-30: no glow at all).
//
// ── Display contrast ──────────────────────────────────────────────────────────
// Dark / Extra Dark raise contrast (contrast_theme/ → BetterDisplay's software filter,
// config.json "dark": 21). It is a gamma table, readable with
// CGGetDisplayTransferByTable: a straight stretch about mid-grey, out = k·in + ½(1 − k)
// (times the brightness scale), k = 1.21 at 21 %. Two things follow for the glow:
//   • on black, everything under x0 = (k − 1)/2k (≈ 22/255) is crushed to black — the
//     faint outer part of the glow vanished there and it looked short and dim;
//   • on grey, the glow's change to the background is multiplied by k — too strong.
// With k > 1 the glow compensates (k is read from the table under Fusion's window,
// every tick): its alpha and colour are divided by k — exact on grey — and a neutral
// lift of up to x0 is added once the glow is present, which carries the black case back
// over the crush point. The lift is the one thing translucency can't keep off grey
// (it lightens grey by about that much), so CONTRAST_COMP says how much of it to take:
// 1 = black exactly as at normal contrast, grey that much lighter; 0 = grey exact, black
// crushed. CONTRAST_TOE ramps the lift in over the glow's faintest alpha, so there is
// no step where the glow begins.
//
// Tuning, live: config/fusion_glow.json — ONE LOOK PER APPEARANCE, keyed light / dark /
// xdark / xlight (the same four contrast_theme/ and Control Room use), each holding any
// of reach, falloff, corner, alpha, rgb [r,g,b 0–255], contrast_comp, contrast_toe.
// Flat top-level keys (the old single-look file) still count, as the base every
// appearance starts from. Re-read (by mtime) on every tick, and again when the
// appearance changes; the image is rebuilt when either does, no restart. Missing keys
// keep the defaults below. One key is NOT per appearance: "fade" (seconds), the
// fade on / off — top level only, a timing rather than a look.
#define D_FADE    0.25
#define D_REACH   120.0
#define D_FALLOFF 1.6
#define D_CORNER  8.0
#define D_ALPHA   0.22
#define D_COMP    0.6
#define D_TOE     0.08
static CGFloat REACH    = D_REACH;    // pt — where the glow has faded to nothing
static CGFloat FALLOFF  = D_FALLOFF;  // (closeness / REACH)^FALLOFF
static CGFloat CORNER_P = D_CORNER;   // corner blend, see above
static CGFloat EDGE_A   = D_ALPHA;    // alpha right at the edge
static CGFloat GLOW_RGB[3] = { 120/255.0, 180/255.0, 1.0 };
static CGFloat CONTRAST_COMP = D_COMP;
static CGFloat CONTRAST_TOE  = D_TOE;  // alpha over which the lift ramps in
static CGFloat CONTRAST_K    = 1.0;    // measured, see contrastUnder()
static CGFloat FADE_S        = D_FADE; // s — fade in / out, top-level "fade"

static NSString *const TUNING = @"/Users/josh/Code/Scripts/config/fusion_glow.json";
static NSString *const EXTRA  = @"/Users/josh/Code/Scripts/fusion_ui/extra.json";

// ── Appearance ────────────────────────────────────────────────────────────────
// light / dark / xdark / xlight: AppleInterfaceStyle + fusion_ui/extra.json, read the
// way contrast_theme/main.swift reads it — `defaults` in a fresh subprocess, NOT
// NSApp.effectiveAppearance (that stayed stale for 26+ minutes in a long-running
// process; main.swift's header). A subprocess per tick would be wasteful, so it is
// refreshed off the main thread about once a second (refreshMode) and the tick just
// reads gMode.
static NSString *gMode = @"light";

static NSString *readMode(void) {
    NSTask *t = [NSTask new];
    t.executableURL = [NSURL fileURLWithPath:@"/usr/bin/defaults"];
    t.arguments = @[@"read", @"-g", @"AppleInterfaceStyle"];
    NSPipe *pipe = [NSPipe pipe];
    t.standardOutput = pipe;
    t.standardError = NSFileHandle.fileHandleWithNullDevice;
    BOOL dark = NO;
    if ([t launchAndReturnError:nil]) {
        NSData *d = [pipe.fileHandleForReading readDataToEndOfFile];
        [t waitUntilExit];
        NSString *s = [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding];
        dark = [[s stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet]
                    isEqualToString:@"Dark"];
    }
    NSData *ed = [NSData dataWithContentsOfFile:EXTRA];
    NSDictionary *ej = ed ? [NSJSONSerialization JSONObjectWithData:ed options:0 error:nil] : nil;
    BOOL extra = [ej isKindOfClass:NSDictionary.class] && [ej[@"on"] boolValue];
    return dark ? (extra ? @"xdark" : @"dark") : (extra ? @"xlight" : @"light");
}

static void refreshMode(void) {
    static BOOL busy = NO;
    if (busy) return;
    busy = YES;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        NSString *m = readMode();
        dispatch_async(dispatch_get_main_queue(), ^{ gMode = m; busy = NO; });
    });
}

static void applyLook(NSDictionary *j) {
    if (![j isKindOfClass:NSDictionary.class]) return;
    NSNumber *v;
    if ((v = j[@"reach"]))   REACH    = MAX(1, v.doubleValue);
    if ((v = j[@"falloff"])) FALLOFF  = MAX(0.05, v.doubleValue);
    if ((v = j[@"corner"]))  CORNER_P = MAX(1, v.doubleValue);
    if ((v = j[@"alpha"]))   EDGE_A   = MIN(1, MAX(0, v.doubleValue));
    if ((v = j[@"contrast_comp"])) CONTRAST_COMP = MIN(1, MAX(0, v.doubleValue));
    if ((v = j[@"contrast_toe"]))  CONTRAST_TOE  = MAX(0.001, v.doubleValue);
    NSArray *rgb = j[@"rgb"];
    if ([rgb isKindOfClass:NSArray.class] && rgb.count == 3)
        for (int i = 0; i < 3; i++) GLOW_RGB[i] = MIN(1, MAX(0, [rgb[i] doubleValue] / 255.0));
}

// Re-read the tuning file when it, or the appearance, changed. YES when the look did.
static BOOL loadTuning(void) {
    static NSDate *seen = nil;
    static NSString *seenMode = nil;
    NSDate *m = [[NSFileManager.defaultManager attributesOfItemAtPath:TUNING error:nil]
                    fileModificationDate];
    if (!m || ([m isEqualToDate:seen] && [gMode isEqualToString:seenMode])) return NO;
    seen = m;
    seenMode = gMode;
    NSDictionary *j = [NSJSONSerialization JSONObjectWithData:[NSData dataWithContentsOfFile:TUNING]
                                                      options:0 error:nil];
    if (![j isKindOfClass:NSDictionary.class]) return NO;
    // From the defaults every time, so one appearance's keys never leak into another's.
    REACH = D_REACH; FALLOFF = D_FALLOFF; CORNER_P = D_CORNER; EDGE_A = D_ALPHA;
    CONTRAST_COMP = D_COMP; CONTRAST_TOE = D_TOE;
    GLOW_RGB[0] = 120/255.0; GLOW_RGB[1] = 180/255.0; GLOW_RGB[2] = 1.0;
    applyLook(j);              // flat keys: the old single-look file, the base
    applyLook(j[gMode]);
    NSNumber *f = j[@"fade"];
    FADE_S = [f isKindOfClass:NSNumber.class] ? MIN(5, MAX(0, f.doubleValue)) : D_FADE;
    return YES;
}

static pid_t gPid = 0;
static NSString *gApp = @"Fusion";   // owner-name substring when no pid; `--app NAME` (psmatte: Photoshop)

// dx, dy: distance in pt to the nearest vertical / horizontal edge.
static CGFloat glowAt(CGFloat dx, CGFloat dy) {
    CGFloat cx = MAX(0, REACH - dx), cy = MAX(0, REACH - dy);
    CGFloat c = MIN(REACH, pow(pow(cx, CORNER_P) + pow(cy, CORNER_P), 1.0 / CORNER_P));
    return pow(c / REACH, FALLOFF);
}

// One premultiplied BGRA pixel for strength g, compensated for CONTRAST_K (see above).
static void glowPixel(uint8_t *p, CGFloat g) {
    CGFloat a0 = EDGE_A * g, k = MAX(1.0, CONTRAST_K), lift = 0;
    if (k > 1.001 && a0 > 0) {
        CGFloat t = MIN(1, a0 / CONTRAST_TOE), x0 = (k - 1) / (2 * k);
        lift = x0 * CONTRAST_COMP * t * t * (3 - 2 * t);           // smoothstep toe
    }
    CGFloat a = MIN(1, a0 / k + lift);
    p[0] = (uint8_t)lround(255 * MIN(a, GLOW_RGB[2] * a0 / k + lift));   // B
    p[1] = (uint8_t)lround(255 * MIN(a, GLOW_RGB[1] * a0 / k + lift));   // G
    p[2] = (uint8_t)lround(255 * MIN(a, GLOW_RGB[0] * a0 / k + lift));   // R
    p[3] = (uint8_t)lround(255 * a);                                     // A
}

// The contrast factor k of the display under `r` (CG coordinates), from its gamma
// table: with T(0) = s·½(1 − k) and T(1) = s·½(1 + k) for brightness s,
// k = (T(1) − T(0)) / (T(1) + T(0)). 1 when there is no stretch (or no table).
static CGFloat contrastUnder(CGRect r) {
    CGDirectDisplayID d; uint32_t n = 0;
    if (CGGetDisplaysWithRect(r, 1, &d, &n) != kCGErrorSuccess || n == 0) d = CGMainDisplayID();
    CGGammaValue red[256], green[256], blue[256]; uint32_t cnt = 0;
    if (CGGetDisplayTransferByTable(d, 256, red, green, blue, &cnt) != kCGErrorSuccess || cnt < 2)
        return 1;
    CGFloat t0 = red[0], t1 = red[cnt - 1];
    if (t1 + t0 <= 1e-4) return 1;
    CGFloat k = (t1 - t0) / (t1 + t0);
    return k > 1.001 ? round(k * 200) / 200 : 1;     // steps of 0.005: no rebuild on noise
}

// A nine-slice: the rim out to REACH on every side around ONE centre pixel. Core
// Animation stretches the middle row/column (contentsCenter) along each edge, which is
// exact here — along an edge nothing changes — so the image stays tiny at any size.
static void freeBuf(void *info, const void *data, size_t size) { free((void *)data); }

static CGImageRef glowImage(CGFloat scale, size_t *outN) {
    size_t r = (size_t)ceil(REACH * scale), n = 2 * r + 1;
    uint8_t *buf = calloc(n * n, 4);
    for (size_t y = 0; y < n; y++)
        for (size_t x = 0; x < n; x++) {
            CGFloat dx = MIN(x + 0.5, n - x - 0.5), dy = MIN(y + 0.5, n - y - 0.5);
            glowPixel(buf + 4 * (y * n + x), glowAt(dx / scale, dy / scale));
        }
    CGColorSpaceRef cs = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGDataProviderRef dp = CGDataProviderCreateWithData(NULL, buf, n * n * 4, freeBuf);
    CGImageRef img = CGImageCreate(n, n, 8, 32, n * 4, cs,
                                   kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little,
                                   dp, NULL, false, kCGRenderingIntentDefault);
    CGDataProviderRelease(dp);
    CGColorSpaceRelease(cs);
    *outN = n;
    return img;          // buf is freed with the image (freeBuf)
}

@interface GlowView : NSView
@property CGFloat builtScale;
@property CGFloat builtK;
@end
@implementation GlowView
- (instancetype)initWithFrame:(NSRect)f {
    if ((self = [super initWithFrame:f])) {
        self.wantsLayer = YES;
        self.layer.contentsGravity = kCAGravityResize;
        self.layer.magnificationFilter = kCAFilterLinear;
    }
    return self;
}
- (BOOL)isOpaque { return NO; }
- (BOOL)wantsUpdateLayer { return YES; }
- (void)viewDidChangeBackingProperties { [self rebuild]; }
- (void)rebuild {
    CGFloat scale = self.window.backingScaleFactor ?: 2.0;
    BOOL tuned = loadTuning();
    if (scale == self.builtScale && CONTRAST_K == self.builtK && !tuned) return;
    self.builtScale = scale;
    self.builtK = CONTRAST_K;
    size_t n;
    CGImageRef img = glowImage(scale, &n);
    self.layer.contents = (__bridge id)img;
    self.layer.contentsScale = scale;
    self.layer.contentsCenter = CGRectMake((n / 2) / (CGFloat)n, (n / 2) / (CGFloat)n,
                                           1.0 / n, 1.0 / n);
    CGImageRelease(img);
}
- (void)updateLayer { [self rebuild]; }
@end

// Fusion's main window in CG (top-left origin) coordinates; NO when none is on screen.
static BOOL fusionBounds(CGRect *out) {
    CFArrayRef list = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
    if (!list) return NO;
    BOOL found = NO;
    CGFloat best = 0;
    for (NSDictionary *w in (__bridge NSArray *)list) {
        if ([w[(id)kCGWindowLayer] intValue] != 0) continue;
        pid_t pid = [w[(id)kCGWindowOwnerPID] intValue];
        if (gPid) { if (pid != gPid) continue; }
        else if (![w[(id)kCGWindowOwnerName] ?: @"" containsString:gApp]) continue;
        CGRect r;
        if (!CGRectMakeWithDictionaryRepresentation(
                (__bridge CFDictionaryRef)w[(id)kCGWindowBounds], &r)) continue;
        CGFloat area = r.size.width * r.size.height;
        if (area > best) { best = area; *out = r; found = YES; }
    }
    CFRelease(list);
    return found;
}

static BOOL fusionFront(void) {
    NSRunningApplication *a = NSWorkspace.sharedWorkspace.frontmostApplication;
    if (!a) return NO;
    if (gPid && a.processIdentifier == gPid) return YES;
    return [a.localizedName ?: @"" containsString:gApp];
}

@interface Controller : NSObject
@property (strong) NSWindow *win;
@property BOOL shown;
@property BOOL dying;   // SIGTERM's fade-out is running — tick must not fade back in
@end
@implementation Controller
- (instancetype)init {
    if ((self = [super init])) {
        NSWindow *w = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 100, 100)
                                                  styleMask:NSWindowStyleMaskBorderless
                                                    backing:NSBackingStoreBuffered
                                                      defer:NO];
        w.releasedWhenClosed = NO;
        w.opaque = NO;
        w.backgroundColor = NSColor.clearColor;
        w.hasShadow = NO;
        w.ignoresMouseEvents = YES;
        // Above FusionDock and the other companion windows (all NSFloatingWindowLevel):
        // at the SAME level the two re-ordered on every click and the glow slid under
        // and over the Browser. Below menus (popUpMenu 101), so those still cover it.
        w.level = NSStatusWindowLevel;
        w.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces
                             | NSWindowCollectionBehaviorStationary
                             | NSWindowCollectionBehaviorFullScreenAuxiliary
                             | NSWindowCollectionBehaviorIgnoresCycle;
        w.contentView = [[GlowView alloc] initWithFrame:NSMakeRect(0, 0, 100, 100)];
        w.alphaValue = 0;
        self.win = w;
    }
    return self;
}
- (void)fade:(BOOL)in {
    if (in == self.shown) return;
    self.shown = in;
    if (in) [self.win orderFrontRegardless];
    [NSAnimationContext runAnimationGroup:^(NSAnimationContext *ctx) {
        ctx.duration = FADE_S;
        self.win.animator.alphaValue = in ? 1 : 0;
    } completionHandler:^{
        if (!self.shown) [self.win orderOut:nil];
    }];
}
- (void)tick {
    if (gPid && kill(gPid, 0) != 0 && errno == ESRCH) exit(0);   // Fusion is gone
    if (self.dying) return;
    CGRect r;
    if (!fusionBounds(&r) || !fusionFront()) { [self fade:NO]; return; }
    CONTRAST_K = contrastUnder(r);
    [(GlowView *)self.win.contentView rebuild];      // a tuning edit, or the contrast moved
    // CG's origin is the top-left of the primary display; Cocoa's its bottom-left.
    CGFloat primaryH = NSScreen.screens.firstObject.frame.size.height;
    NSRect f = NSMakeRect(r.origin.x, primaryH - r.origin.y - r.size.height,
                          r.size.width, r.size.height);
    if (!NSEqualRects(f, self.win.frame)) [self.win setFrame:f display:YES];
    [self fade:YES];
}
@end

int main(int argc, char **argv) {
    @autoreleasepool {
        // fusion_glow --png <out.png> [w h [k [mode]]] — the glow (mode's look; default the
        // current appearance) composited the way the window server does it
        // (out = src + (1 − a)·dst) over bands of black, dark grey, mid grey and white,
        // for tuning the look without a live window (1x, no Fusion).
        // Three rows, each a w×h card: normal contrast (the target); contrast k (default
        // 1.21) with NO compensation; contrast k compensated. The last two are passed
        // through the display's stretch, so they show what the screen would.
        if (argc > 2 && strcmp(argv[1], "--png") == 0) {
            gMode = argc > 6 ? @(argv[6]) : readMode();
            loadTuning();
            long w = argc > 4 ? atol(argv[3]) : 900, h = argc > 4 ? atol(argv[4]) : 300;
            CGFloat k = argc > 5 ? atof(argv[5]) : 1.21;
            NSBitmapImageRep *rep = [[NSBitmapImageRep alloc]
                initWithBitmapDataPlanes:NULL pixelsWide:w pixelsHigh:h * 3 bitsPerSample:8
                samplesPerPixel:3 hasAlpha:NO isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace
                bytesPerRow:w * 3 bitsPerPixel:24];
            const double bands[4] = { 0.0, 0.12, 0.35, 1.0 };
            const CGFloat profileK[3] = { 1, 1, k }, displayK[3] = { 1, k, k };
            for (int row = 0; row < 3; row++) {
                CONTRAST_K = profileK[row];
                CGFloat dk = displayK[row];
                for (long y = 0; y < h; y++)
                    for (long x = 0; x < w; x++) {
                        CGFloat dx = MIN(x + 0.5, w - x - 0.5), dy = MIN(y + 0.5, h - y - 0.5);
                        uint8_t px[4];
                        glowPixel(px, glowAt(dx, dy));
                        double bg = bands[MIN(3, x * 4 / w)], a = px[3] / 255.0;
                        uint8_t *o = rep.bitmapData + ((row * h) + y) * w * 3 + x * 3;
                        for (int c = 0; c < 3; c++) {      // px is BGRA; o is RGB
                            double v = MIN(1, px[2 - c] / 255.0 + (1 - a) * bg);
                            v = dk * v + 0.5 * (1 - dk);    // the display's stretch
                            o[c] = (uint8_t)lround(255 * MIN(1, MAX(0, v)));
                        }
                    }
            }
            [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}]
                writeToFile:@(argv[2]) atomically:YES];
            return 0;
        }
        int ai = 1;
        if (argc > 2 && strcmp(argv[1], "--app") == 0) { gApp = @(argv[2]); ai = 3; }
        if (argc > ai) gPid = (pid_t)atoi(argv[ai]);
        NSApplication *app = NSApplication.sharedApplication;
        app.activationPolicy = NSApplicationActivationPolicyProhibited;  // no Dock, no focus
        Controller *c = [Controller new];
        // SIGTERM fades out first, so switching the mode off looks like switching it on.
        signal(SIGTERM, SIG_IGN);
        dispatch_source_t term = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, SIGTERM, 0,
                                                        dispatch_get_main_queue());
        dispatch_source_set_event_handler(term, ^{
            c.dying = YES;
            [c fade:NO];
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)((FADE_S + 0.05) * NSEC_PER_SEC)),
                           dispatch_get_main_queue(), ^{ exit(0); });
        });
        dispatch_resume(term);
        gMode = readMode();
        [NSTimer scheduledTimerWithTimeInterval:POLL_S repeats:YES
                                          block:^(NSTimer *t) { [c tick]; }];
        [NSTimer scheduledTimerWithTimeInterval:1.0 repeats:YES
                                          block:^(NSTimer *t) { refreshMode(); }];
        [c tick];
        [app run];
    }
    return 0;
}
