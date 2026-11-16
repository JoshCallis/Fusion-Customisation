/*
 * fusion_busy — a small spinner beside the pointer while Fusion's main thread is busy.
 *
 * You click, run a command or open a sketch, and Fusion (or one of our add-ins, which
 * all run on its main thread) works for a second or more with nothing on screen to
 * say the click landed: macOS only shows its beachball after ~2 s. This says so at
 * once — from OUTSIDE Fusion, so it covers everything: Fusion's own recomputes, our
 * add-ins' handlers, a garbage collection, whatever holds the main thread.
 *
 * How busy is seen: a ping thread asks Fusion's application element for its role over
 * Accessibility every PING_EVERY_S. AppKit answers AX requests on the main thread, so
 * an answer means the main thread got round to it; no answer within PING_TIMEOUT_S
 * means it is stuck in something. Only while Fusion (or one of its companion windows —
 * any frontmost app named "*Fusion*") is in front; otherwise nothing is asked.
 *
 * The spinner: one borderless, click-through, shadowless window at the status level,
 * a dark disc with a light spinner (readable on light and dark canvases), kept just
 * below-right of the pointer at 60 Hz while shown. Shown on the first missed ping,
 * hidden on the first answer — but kept MIN_SHOW_S so a short hitch is a blink, not a
 * flicker. Each busy spell is logged with its length to /tmp/fusion_busy.log — a
 * record of every hitch over ~0.15 s, for finding the next thing to make faster.
 *
 * Needs Accessibility, once: System Settings ▸ Privacy & Security ▸ Accessibility ▸
 * /Users/josh/Code/Scripts/fusion_busy (path-keyed, like remapd — hence the repo
 * root). Without it the log says so and nothing is shown. Run by launchd:
 * ~/Library/LaunchAgents/com.josh.fusion-busy.plist (config/launchd/).
 *
 * Off switch: config/fusion_busy_off present = never armed — the Busy Spinner tick on
 * the Browser's right-click menu writes it.
 *
 * Build: bin/build.sh fusion_busy
 */
#import <Cocoa/Cocoa.h>
#import <ApplicationServices/ApplicationServices.h>
#include <stdatomic.h>

static const NSTimeInterval PING_EVERY_S = 0.10;   // between pings while Fusion is in front
static const float PING_TIMEOUT_S = 0.15f;         // unanswered this long = busy
static const NSTimeInterval MIN_SHOW_S = 0.25;     // a shown spinner stays at least this long
static const CGFloat SIZE = 26;                    // the disc, points
static const CGFloat OFFSET = 14;                  // from the pointer's hot spot
static NSString *const FUSION_ID = @"com.autodesk.fusion360";
static const char *LOG_PATH = "/tmp/fusion_busy.log";
// Present = off (the Browser menu tail's Busy Spinner tick, fusion_browser/server.py).
static const char *OFF_PATH = "/Users/josh/Code/Scripts/config/fusion_busy_off";

static _Atomic pid_t g_pid = 0;      // Fusion's pid while it (or a companion) is in front
static _Atomic bool g_busy = false;  // the ping thread's verdict

static void logf_(const char *fmt, ...) {
    FILE *f = fopen(LOG_PATH, "a");
    if (!f) return;
    char ts[32];
    time_t t = time(NULL);
    strftime(ts, sizeof ts, "%H:%M:%S", localtime(&t));
    fprintf(f, "%s ", ts);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

// ── The ping ──────────────────────────────────────────────────────────────────
static void pingLoop(void) {
    AXUIElementRef app = NULL;
    pid_t appPid = 0;
    bool saidNoGrant = false;
    while (true) {
        @autoreleasepool {
            pid_t pid = atomic_load(&g_pid);
            if (!pid) {
                atomic_store(&g_busy, false);
                [NSThread sleepForTimeInterval:0.25];
                continue;
            }
            if (pid != appPid) {
                if (app) CFRelease(app);
                app = AXUIElementCreateApplication(pid);
                appPid = pid;
                if (app) AXUIElementSetMessagingTimeout(app, PING_TIMEOUT_S);
            }
            CFTypeRef v = NULL;
            CFAbsoluteTime t0 = CFAbsoluteTimeGetCurrent();
            AXError err = app ? AXUIElementCopyAttributeValue(app, kAXRoleAttribute, &v)
                              : kAXErrorFailure;
            CFAbsoluteTime dt = CFAbsoluteTimeGetCurrent() - t0;
            if (v) CFRelease(v);
            if (err == kAXErrorAPIDisabled) {
                if (!saidNoGrant) {
                    logf_("no Accessibility grant — add /Users/josh/Code/Scripts/fusion_busy "
                          "in System Settings > Privacy & Security > Accessibility");
                    saidNoGrant = true;
                }
                atomic_store(&g_busy, false);
                [NSThread sleepForTimeInterval:5.0];
                continue;
            }
            saidNoGrant = false;
            atomic_store(&g_busy, err == kAXErrorCannotComplete || dt >= PING_TIMEOUT_S);
            // Busy, the ping already waited out its timeout; idle, keep the cadence.
            NSTimeInterval rest = PING_EVERY_S - dt;
            if (rest > 0) [NSThread sleepForTimeInterval:rest];
        }
    }
}

// ── The spinner ───────────────────────────────────────────────────────────────
@interface Busy : NSObject
@property (strong) NSPanel *win;
@property (strong) NSProgressIndicator *spin;
@property (assign) BOOL shown;
@property (assign) CFAbsoluteTime shownAt;
@property (assign) CFAbsoluteTime busySince;
@end

@implementation Busy

- (instancetype)init {
    if (!(self = [super init])) return nil;
    NSRect r = NSMakeRect(0, 0, SIZE, SIZE);
    NSPanel *w = [[NSPanel alloc] initWithContentRect:r
                                            styleMask:NSWindowStyleMaskBorderless |
                                                      NSWindowStyleMaskNonactivatingPanel
                                              backing:NSBackingStoreBuffered defer:NO];
    w.opaque = NO;
    w.backgroundColor = NSColor.clearColor;
    w.hasShadow = NO;
    w.ignoresMouseEvents = YES;
    w.level = NSStatusWindowLevel + 1;
    w.hidesOnDeactivate = NO;
    w.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces |
                           NSWindowCollectionBehaviorStationary |
                           NSWindowCollectionBehaviorIgnoresCycle |
                           NSWindowCollectionBehaviorFullScreenAuxiliary;
    NSView *disc = [[NSView alloc] initWithFrame:r];
    disc.wantsLayer = YES;
    disc.layer.backgroundColor = [NSColor colorWithWhite:0.08 alpha:0.78].CGColor;
    disc.layer.cornerRadius = SIZE / 2;
    NSProgressIndicator *p = [[NSProgressIndicator alloc]
                              initWithFrame:NSInsetRect(r, 5, 5)];
    p.style = NSProgressIndicatorStyleSpinning;
    p.controlSize = NSControlSizeSmall;
    p.displayedWhenStopped = NO;
    p.appearance = [NSAppearance appearanceNamed:NSAppearanceNameDarkAqua];
    [disc addSubview:p];
    w.contentView = disc;
    _win = w;
    _spin = p;
    return self;
}

- (void)place {
    NSPoint m = [NSEvent mouseLocation];                // bottom-left origin
    [self.win setFrameOrigin:NSMakePoint(m.x + OFFSET, m.y - OFFSET - SIZE)];
}

- (void)tick {
    CFAbsoluteTime now = CFAbsoluteTimeGetCurrent();
    bool busy = atomic_load(&g_busy);
    if (busy && !self.busySince) self.busySince = now;
    if (!busy && self.busySince) {
        logf_("busy %.2f s", now - self.busySince);
        self.busySince = 0;
    }
    if (busy && !self.shown) {
        [self place];
        [self.spin startAnimation:nil];
        [self.win orderFrontRegardless];
        self.shown = YES;
        self.shownAt = now;
    } else if (!busy && self.shown && now - self.shownAt >= MIN_SHOW_S) {
        [self.spin stopAnimation:nil];
        [self.win orderOut:nil];
        self.shown = NO;
    } else if (self.shown) {
        [self place];
    }
}

// Which app is in front: Fusion itself or one of its companion windows (their names
// all hold "Fusion" — FusionDock, FusionValue…) arms the ping, at Fusion's pid.
- (void)front {
    NSRunningApplication *f = NSWorkspace.sharedWorkspace.frontmostApplication;
    pid_t pid = 0;
    if (access(OFF_PATH, F_OK) == 0) f = nil;           // switched off: never armed
    if (f && ([f.bundleIdentifier isEqualToString:FUSION_ID] ||
              [f.localizedName rangeOfString:@"Fusion"].location != NSNotFound)) {
        if ([f.bundleIdentifier isEqualToString:FUSION_ID]) {
            pid = f.processIdentifier;
        } else {
            NSArray *a = [NSRunningApplication
                          runningApplicationsWithBundleIdentifier:FUSION_ID];
            if (a.count) pid = [a.firstObject processIdentifier];
        }
    }
    atomic_store(&g_pid, pid);
}

@end

int main(void) {
    @autoreleasepool {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
        Busy *b = [Busy new];
        [b front];
        [[NSWorkspace.sharedWorkspace notificationCenter]
            addObserverForName:NSWorkspaceDidActivateApplicationNotification object:nil
                         queue:NSOperationQueue.mainQueue
                    usingBlock:^(NSNotification *n) { [b front]; }];
        [NSTimer scheduledTimerWithTimeInterval:0.5 repeats:YES
                                          block:^(NSTimer *t) { [b front]; }];
        [NSTimer scheduledTimerWithTimeInterval:1.0 / 60 repeats:YES
                                          block:^(NSTimer *t) { [b tick]; }];
        [NSThread detachNewThreadWithBlock:^{ pingLoop(); }];
        logf_("started%s", AXIsProcessTrusted() ? "" : " (no Accessibility grant yet)");
        [NSApp run];
    }
    return 0;
}
