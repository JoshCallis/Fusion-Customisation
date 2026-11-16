/*
 * sketch_palette — find Fusion's Sketch Palette window and move it off-screen.
 *
 * The Sketch Palette (grid/snap/slice/Look At options + Finish Sketch) is a
 * separate NSPanel Fusion creates fresh every time a sketch is opened for
 * edit — confirmed live: `win_list`/AX both see it as its own ~250×466 window
 * at a fixed corner of the canvas, distinct from the canvas window itself.
 * There is no API to hide it (checked: not on Sketch, not a `ui.palettes`
 * entry — those are only add-in-created HTML palettes), so this repositions
 * it past every display via cross-process AX, the same technique axPosition()
 * in scene_restore.m uses to move Fusion's own canvas. The window is still
 * live off-screen — Finish Sketch, Snap, etc. all keep working — it is just
 * not visually in the way.
 *
 * Identified by: NOT the app's AXMain window (that's always the canvas — see
 * scene_restore.m's axPosition() for why AXMain, not subrole, is the
 * discriminator: Maximize Viewport's borderless canvas reports AXDialog, same
 * as a palette) AND an empty AXTitle (Fusion never titles the palette; every
 * other Fusion window/dialog observed has one). A false-empty match would at
 * worst move some other small untitled dialog off-screen harmlessly.
 *
 * Usage: sketch_palette hide [settle_ms] | show
 *
 * Build/sign: bin/build.sh (target "sketch_palette", deployed to ~/bin).
 */
#import <Cocoa/Cocoa.h>
#import <ApplicationServices/ApplicationServices.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#define FUSION_BUNDLE_ID @"com.autodesk.fusion360"

// Private but long-stable (yabai, Hammerspoon): the CGWindowID behind an AX window.
extern AXError _AXUIElementGetWindow(AXUIElementRef, CGWindowID *);

static pid_t fusion_pid(void) {
    for (NSRunningApplication *a in [[NSWorkspace sharedWorkspace] runningApplications]) {
        if ([a.bundleIdentifier isEqualToString:FUSION_BUNDLE_ID]) return a.processIdentifier;
    }
    return 0;
}

// Just past the right edge of every attached display, so the palette lands
// off-screen regardless of how many monitors are arranged where.
static CGFloat offscreen_x(void) {
    CGFloat maxX = 0;
    for (NSScreen *s in [NSScreen screens]) {
        CGFloat right = s.frame.origin.x + s.frame.size.width;
        if (right > maxX) maxX = right;
    }
    return maxX + 100;
}

// Finds the palette (non-main, untitled window) and the canvas (AXMain).
// Returned refs are retained; caller releases. Either may come back NULL.
static void find_windows(AXUIElementRef app, AXUIElementRef *palette, AXUIElementRef *canvas) {
    *palette = NULL; *canvas = NULL;
    CFArrayRef wins = NULL;
    if (AXUIElementCopyAttributeValue(app, kAXWindowsAttribute,
                                      (CFTypeRef *)&wins) != kAXErrorSuccess || !wins) return;
    CFIndex n = CFArrayGetCount(wins);
    for (CFIndex i = 0; i < n; i++) {
        AXUIElementRef w = (AXUIElementRef)CFArrayGetValueAtIndex(wins, i);

        BOOL isMain = NO;
        CFBooleanRef mainv = NULL;
        if (AXUIElementCopyAttributeValue(w, kAXMainAttribute,
                                          (CFTypeRef *)&mainv) == kAXErrorSuccess && mainv) {
            isMain = CFBooleanGetValue(mainv);
            CFRelease(mainv);
        }
        if (isMain) {   // the canvas — never the palette
            if (!*canvas) *canvas = (AXUIElementRef)CFRetain(w);
            continue;
        }

        BOOL emptyTitle = NO;
        CFStringRef title = NULL;
        if (AXUIElementCopyAttributeValue(w, kAXTitleAttribute,
                                          (CFTypeRef *)&title) == kAXErrorSuccess && title) {
            emptyTitle = (CFStringGetLength(title) == 0);
            CFRelease(title);
        }
        if (emptyTitle && !*palette) *palette = (AXUIElementRef)CFRetain(w);
    }
    CFRelease(wins);
}

static BOOL ax_point(AXUIElementRef w, CFStringRef attr, AXValueType type, void *out) {
    AXValueRef v = NULL;
    if (AXUIElementCopyAttributeValue(w, attr, (CFTypeRef *)&v) != kAXErrorSuccess || !v) return NO;
    BOOL ok = AXValueGetValue(v, type, out);
    CFRelease(v);
    return ok;
}

static void set_position(AXUIElementRef w, CGPoint p) {
    AXValueRef av = AXValueCreate(kAXValueTypeCGPoint, &p);
    AXUIElementSetAttributeValue(w, kAXPositionAttribute, av);
    CFRelease(av);
}

// Fusion's own spot for the palette: canvas top-right, inset 4pt (measured
// live — canvas at 0,0 3440 wide put the palette at 3186,4 for a 250 wide one).
static CGPoint home_position(AXUIElementRef canvas, AXUIElementRef palette) {
    CGPoint cp = {0, 0}; CGSize cs = {0, 0}; CGSize ps = {250, 466};
    ax_point(canvas, kAXPositionAttribute, kAXValueTypeCGPoint, &cp);
    ax_point(canvas, kAXSizeAttribute, kAXValueTypeCGSize, &cs);
    ax_point(palette, kAXSizeAttribute, kAXValueTypeCGSize, &ps);
    return (CGPoint){ cp.x + cs.width - ps.width - 4, cp.y + 4 };
}

/*
 * hide [settle_ms]  — move the palette off-screen. With settle_ms, keep
 *                     watching for that long and re-hide whenever Fusion puts
 *                     it back: Fusion creates/places the palette some time
 *                     AFTER SketchActivate/SketchCreate terminate, so a single
 *                     shot fired on that event races and often loses.
 * show              — put it back at Fusion's own spot (canvas top-right).
 *
 * hide prints the palette's window number (one line, first sighting of each
 * window) on stdout. The Hide Sketch Palette add-in reads it and, from inside
 * Fusion's process, makes that NSWindow transparent and click-through — so when
 * Fusion puts the palette back on-screen (every command dialog does) there is
 * nothing to see in the beat before this loop moves it off again.
 */
int main(int argc, const char *argv[]) {
    BOOL hide = argc >= 2 && strcmp(argv[1], "hide") == 0;
    BOOL show = argc >= 2 && strcmp(argv[1], "show") == 0;
    if (!hide && !show) {
        fprintf(stderr, "usage: sketch_palette hide [settle_ms] | show\n");
        return 2;
    }
    // show retries briefly too — AX occasionally misses the window for a beat.
    int settle_ms = hide ? (argc >= 3 ? atoi(argv[2]) : 0) : 300;

    @autoreleasepool {
        pid_t pid = fusion_pid();
        if (pid == 0) {
            fprintf(stderr, "sketch_palette: Fusion is not running\n");
            return 1;
        }
        AXUIElementRef app = AXUIElementCreateApplication(pid);
        CGFloat offX = offscreen_x();
        BOOL moved = NO;
        CGWindowID reported = 0;

        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:settle_ms / 1000.0];
        do {
            AXUIElementRef palette = NULL, canvas = NULL;
            find_windows(app, &palette, &canvas);
            if (palette && hide) {
                CGWindowID wid = 0;
                if (_AXUIElementGetWindow(palette, &wid) == kAXErrorSuccess && wid && wid != reported) {
                    printf("%u\n", wid);
                    fflush(stdout);
                    reported = wid;
                }
            }
            if (palette) {
                CGPoint p = {0, 0};
                ax_point(palette, kAXPositionAttribute, kAXValueTypeCGPoint, &p);
                if (hide && p.x < offX - 1) {
                    set_position(palette, (CGPoint){ offX, 0 });
                    moved = YES;
                } else if (hide) {
                    moved = YES;   // already off-screen
                } else if (canvas) {
                    set_position(palette, home_position(canvas, palette));
                    moved = YES;
                }
            }
            if (palette) CFRelease(palette);
            if (canvas) CFRelease(canvas);
            if (settle_ms > 0) usleep(40 * 1000);
        } while (settle_ms > 0 && !(show && moved) && [deadline timeIntervalSinceNow] > 0);

        CFRelease(app);
        if (!moved) {
            fprintf(stderr, "sketch_palette: no palette window found (not editing a sketch?)\n");
            return 1;
        }
        return 0;
    }
}
