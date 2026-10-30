r"""Drive the RWD Olfactometer (Olfactometer.exe).

The Olfactometer shows a device-check dialog ("暂无通道" / no channel) with a
"重试" (retry) button that makes the application retry the serial connection.
The button is clicked directly at a fixed position, so no UI Automation tree
walk is needed - walking the UIA tree of this WPF application takes ~2.5s,
which would stall the tracker.

The retry button always sits at the same place inside the dialog, and the
dialog is centred on the Olfactometer window, so the click position is
derived from the window rectangle.  The window is located by its title with
the plain Win32 API (~1ms).

The Olfactometer window is larger than the display it runs on, so the retry
button can end up below the bottom edge of the screen where no mouse click
can reach it.  The window is therefore moved up before the click, and moved
back afterwards.
"""

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

from pywinauto import mouse

# The Olfactometer runs at 192 DPI (200% scaling), so the coordinates that
# the window and the mouse use are physical pixels.  A process that is not
# DPI aware sees a screen of half that size, which would make every
# position look off-screen.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

# Title of the Olfactometer main window.
WINDOW_TITLE = "Olfactometer"

# Offset of the "重试" (retry) button from the centre of the window.
RETRY_OFFSET = (-200, 131)

# Held while a click is in flight, so that repeated calls cannot pile up
# threads.  Acquired without blocking: a call that finds it busy is skipped.
_busy = threading.Lock()


def _main_window():
    """Return the handle of the Olfactometer window, or 0 if not found."""
    user32 = ctypes.windll.user32
    found = []

    def callback(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == WINDOW_TITLE and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(callback), 0)
    return found[0] if found else 0


def _window_rect(hwnd):
    """Return the rectangle of the window as (left, top, right, bottom)."""

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _retry_position(hwnd):
    """Return the screen position of the "重试" (retry) button."""
    left, top, right, bottom = _window_rect(hwnd)
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    return center_x + RETRY_OFFSET[0], center_y + RETRY_OFFSET[1]


def _screen_size():
    """Return the size of the virtual screen as (width, height)."""
    user32 = ctypes.windll.user32
    return user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)


def _cursor_pos():
    """Return the current mouse position as (x, y)."""

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def _dialog_window():
    """Return the handle of the device-check dialog, or 0 if not shown.

    The dialog is a separate top-level window of the application, centred
    on the main window.  It has no title of its own, so it is found by its
    position: it is smaller than the main window and centred on it.
    """
    user32 = ctypes.windll.user32
    main = _main_window()
    if not main:
        return 0

    left, top, right, bottom = _window_rect(main)
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    found = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or hwnd == main:
            return True
        h_l, h_t, h_r, h_b = _window_rect(hwnd)
        width, height = h_r - h_l, h_b - h_t
        # The dialog is roughly a quarter of the main window and centred.
        if width < (right - left) // 2 and height < (bottom - top) // 2:
            if (
                abs((h_l + h_r) // 2 - center_x) < 40
                and abs((h_t + h_b) // 2 - center_y) < 40
            ):
                found.append(hwnd)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(callback), 0)
    return found[0] if found else 0


def _foreground(hwnd):
    """Bring a window to the front and wait until it really is.

    SetForegroundWindow only asks, and Windows ignores the request when it
    comes from a thread that does not own the foreground.  The click would
    then land on whatever is on top instead.  Attaching to the thread of
    the current foreground window lifts that restriction.
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    current = user32.GetForegroundWindow()
    current_tid = user32.GetWindowThreadProcessId(current, None)
    own_tid = kernel32.GetCurrentThreadId()

    attached = False
    if current and current_tid != own_tid:
        attached = bool(user32.AttachThreadInput(own_tid, current_tid, True))

    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(own_tid, current_tid, False)

    for _ in range(20):
        time.sleep(0.05)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


def _run(action, timeout):
    """Start ``action`` in a worker thread and return immediately.

    Returns False when another action is still running, in which case this
    one is skipped.
    """
    if not _busy.acquire(blocking=False):
        logging.info("Olfactometer: action already running, skipped")
        return False

    def worker():
        try:
            action()
        except Exception as exc:
            logging.warning("Olfactometer: %s", exc)
        finally:
            _busy.release()

    threading.Thread(target=worker, name="olfactometer-ui", daemon=True).start()
    return True


def click_retry(timeout: float = 5.0) -> bool:
    """Click the Olfactometer "重试" (retry) button.

    Runs in the background and returns immediately, so the tracker never
    stalls.  Returns False when a click is already in flight.  Never raises:
    a failing trigger must not abort the trial.
    """

    x, y = 452, 774

    mouse.click(coords=(x, y))
    logging.info("Olfactometer: retry button clicked at (%d, %d)", x, y)

    return True
