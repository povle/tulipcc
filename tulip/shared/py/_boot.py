import gc
import uos
import tulip, sys, midi, synth, amy, world, sequencer
from upysh import *
from tulip import board

if board()!="AMYBOARD" and board()!="AMYBOARD_WEB" and board()!="AMYBOARD_VCV" and board()!="CYNTHIA":
    from tulip import edit, run
else:
    if board()=="AMYBOARD":
        import amyboard
        amyboard.mount_sd()
        edit = amyboard.edit
    elif board()=="CYNTHIA":
        import amyboard
    elif board()=="AMYBOARD_VCV":
        import amyboard

if board()=="WEB" or board()=="AMYBOARD_WEB":
    def webnyi():
        return "This function is not available on Tulip Web"
    import world_web as world
    input = webnyi()
else:
    import world

# This _boot runs both desktop and esp32s3


try:
    from esp32 import Partition
    try:
        s = Partition.find(Partition.TYPE_DATA, label="system")[0]
        u = Partition.find(Partition.TYPE_DATA, label="vfs")[0]
        try:
            uos.mount(s,"/sys")
        except OSError:
            # First boot: format the system partition and retry
            print("Formatting system partition...")
            uos.VfsFat.mkfs(s)
            uos.mount(s,"/sys")
        try:
            uos.mount(u,"/user")
        except OSError:
            # First boot: format the vfs partition and retry
            print("Formatting user partition...")
            uos.VfsFat.mkfs(u)
            uos.mount(u,"/user")
        cd('/user')
        sys.path.append("/sys/ex")
        
    except Exception as e:
        print("_boot: partition error:", e)
        print("Tulip flash not setup properly. Try resetting the device.")

    try:
        currentPartition = Partition(Partition.RUNNING)
        currentPartition.mark_app_valid_cancel_rollback()
    except OSError:
        # this is you booting a non-OTA esp32 device. it's ok!
        pass


except ImportError:
    # Not a hardware Tulip
    try:
        tulipcc = tulip.root_dir()
        if not tulip.exists(tulipcc):
            mkdir(tulipcc)
    
        if tulip.board()=='AMYBOARD_VCV':
            try:
                mkdir(tulipcc+"user")
            except OSError:
                pass

        # On Desktop, we can put sys in sys/ and user in user/
        if tulip.board()=='DESKTOP':
            try:
                mkdir(tulipcc+"sys")
                tulip.desktop_copy_sys(tulipcc+"sys")
            except OSError:
                # already exists, do nothing
                pass

            try:
                mkdir(tulipcc+"user")
            except OSError:
                # Already exists, don't do anything
                pass

        cd(tulip.root_dir()+"user")
        tulip.add_to_bootpy("# boot.py\n# Put anything here you want to run on Tulip startup\n", only_first_create=True)
        sys.path.append("../sys/ex")

    except Exception as e:
        sys.print_exception(e)
        # Probably iOS
        cd(tulip.app_path())

# Make sure user/lib exists and add it to sys.path
libdir = tulip.root_dir()+"user/lib"
if not tulip.exists(libdir):
    mkdir(libdir)
sys.path.append(libdir)

# Remove other ones
try:
    sys.path.remove('/lib')
except ValueError:
    pass
try:
    sys.path.remove('~/.micropython/lib')
except ValueError:
    pass

gc.collect()

# Set up audio/midi. 
if tulip.board() == "WEB" or tulip.board()=="AMYBOARD_WEB":
    amy.AMY_SAMPLE_RATE=48000
    # Override send & bleep are done from JS on web because of click-to-start audio.
else:
    amy.AMY_SAMPLE_RATE=44100
    # Nothing to override: amy's own dispatch already resolves _send_wire to
    # tulip.amy_send here (amy_c_api_mp.inc). Installing the platform default
    # in amy.override_send would only make it look like the user redirected
    # AMY elsewhere, which is how transfer chunks lost their sysex marking and
    # got parsed as wire commands (amy #1045). override_send stays free for
    # what it's for -- pointing AMY at another board over midi.sysex_amy/i2c.

# amy.message() in C: tulip.amy_message() (amy_connector.c) builds the same
# wire string several times faster -- a table walk into a stack buffer instead
# of per-argument map slicing and `+=`, which all allocate. Installed over
# amy.message here so every amy.send() on every target gets it; the Python
# original is captured FIRST and the fallback goes to it by name, or the
# fallback would find this function again and recurse.
#
# The C side returns None for anything it does not reproduce exactly (an
# unknown keyword, a numeric string where an int was meant, an overlong
# message) and the Python original then runs -- and raises whatever it was
# going to raise. Kwarg combos that amy.message() warns or errors about while
# amy.show_warnings is set go straight to Python so those diagnostics still
# fire.
_py_amy_message = amy.message
amy._py_message = _py_amy_message   # the original, reachable for tests/debugging
def _c_amy_message(**kwargs):
    if amy.show_warnings and ('patch_string' in kwargs or 'num_partials' in kwargs or
            ('voices' in kwargs and ('preset' in kwargs or 'synth' in kwargs))):
        return _py_amy_message(**kwargs)
    m = tulip.amy_message(**kwargs)
    if m is None:
        return _py_amy_message(**kwargs)
    return m
amy.message = _c_amy_message

if board() == "AMYBOARD" or board()=="AMYBOARD_WEB" or board()=="AMYBOARD_VCV":
    import amyboard
    amyboard.ensure_user_environment()
    amyboard.init_display()

if board() == "CYNTHIA":
    import amyboard
    # Guarded: if /user is unwritable or the layout cannot be created, boot must
    # still reach the REPL rather than dying here (nothing after this would run,
    # including the watchdog and the guard).
    try:
        amyboard.ensure_user_environment()
    except Exception as _ee:
        print("_boot: CYNTHIA could not prepare /user:", _ee)
    # CYNTHIA is headless - skip init_display()
    # CYNTHIA boot flow (§17): restore the last-selected instrument's
    # sketch.py into /user/current/ BEFORE start_amy() runs it. The
    # interactive browser is a REPL command (cynthia.browser.run()) —
    # boot must not block on input() headless.
    #
    # Boot guard first: count this boot BEFORE doing anything that can crash, so
    # a boot that never completes is visible on the next one. See cynthia/guard.py
    # for why a *file* and not RTC_NOINIT (it has to survive a power cycle).
    try:
        from cynthia import guard as _guard
    except Exception as _ge:
        _guard = None
        print("_boot: CYNTHIA guard unavailable:", _ge)
    if _guard is not None:
        _skipped = False
        try:
            from cynthia import browser as _b
            # Two independent ways a sketch can keep the board from being usable,
            # and they need separate counters because they look different from
            # here (see cynthia/guard.py):
            #
            #  (a) the boot never COMPLETES — note_boot() runs before the restore,
            #      and guard.clear() only at the very end of this block;
            #  (b) the boot completes and the sketch wedges LATER, so the task
            #      watchdog resets the board. Counter (a) sees a perfectly healthy
            #      boot in that case, so the reset cause has to be consulted.
            #
            # Both funnel into one safe-mode path so the neutralisation below
            # cannot be forgotten for one of them (flaw 3 §12b).
            _why = None
            _via_wdt = False
            # Did the board recover itself from a hang? The software watchdog
            # records why before it reboots (see cynthia_wdt.h) because
            # esp_restart() reports ESP_RST_SW, not ESP_RST_TASK_WDT — so
            # reset_cause() alone cannot tell a watchdog reboot from a normal
            # one. take_reason() is read-and-clear.
            try:
                if hasattr(tulip, "cynthia_wdt_reason"):
                    _wr = tulip.cynthia_wdt_reason()
                    if _wr:
                        _w = _guard.note_wdt_boot()
                        print("_boot: CYNTHIA recovered from a hang "
                              "(software watchdog, reason=0x%08x, %d in a row)"
                              % (_wr, _w))
                        if _guard.in_wdt_safe_mode():
                            _why = "repeated watchdog recoveries"
                            _via_wdt = True
            except Exception as _we:
                print("_boot: CYNTHIA watchdog reason check failed:", _we)

            try:
                import machine as _m
                if _m.reset_cause() == _m.WDT_RESET:
                    _w = _guard.note_wdt_boot()
                    print("_boot: CYNTHIA previous boot was an IDF watchdog reset (%d)"
                          % _w)
                    if _guard.in_wdt_safe_mode():
                        _why = "repeated watchdog resets"
                        _via_wdt = True
            except Exception as _we:
                print("_boot: CYNTHIA WDT check failed:", _we)

            _n = _guard.note_boot(_b.last_instrument())
            if _guard.in_safe_mode():
                _why = "boot did not complete %d times" % _n
                _via_wdt = False

            if _why:
                _culprit = _guard.last_instrument()
                print("_boot: CYNTHIA SAFE MODE (%s)" % _why)
                print("_boot: skipped restoring %r — it is likely the cause" % _culprit)
                print("_boot: fix it, or run cynthia.guard.reset() to try again")
                print("_boot: browse instruments with cynthia.browser.run()")
                # Suspend the culprit AND clear the counters: safe mode is a
                # transition, not a state. Leaving a counter high would keep every
                # later boot in safe mode (a REPL-only brick).
                # Use the trigger we recorded, not a re-test of state — re-testing
                # could pick the wrong counter to clear.
                if _via_wdt:
                    _guard.enter_wdt_safe_mode(_culprit)
                else:
                    _guard.enter_safe_mode(_culprit)
                # Both entries clear BOTH counters, so whichever fired the next
                # boot starts clean (rather than re-entering safe mode forever).
                # AND neutralise the stale file. Skipping restore_last() is not
                # enough on its own: /user/current/sketch.py still holds whatever
                # the last boot wrote there, and amyboard.start_amy() below runs
                # *that* file regardless — so the failure would simply repeat and
                # the guard would never converge (flaw 3 §12b).
                try:
                    with open("/user/current/sketch.py", "w") as _f:
                        _f.write(amyboard.DEFAULT_SKETCH_SOURCE)
                    print("_boot: reset /user/current/sketch.py to the default")
                except Exception as _re:
                    print("_boot: could not reset current sketch:", _re)
                _skipped = True
        except Exception as _ge2:
            print("_boot: CYNTHIA guard failed:", _ge2)
            _skipped = False
    else:
        _skipped = False
    if not _skipped:
        try:
            from cynthia import browser as _browser
            _last = _browser.restore_last()
            if _last:
                print("_boot: CYNTHIA restoring instrument %r" % _last)
            else:
                print("_boot: CYNTHIA no instrument yet — run cynthia.browser.run() at the REPL")
        except Exception as _e:
            print("_boot: CYNTHIA restore failed:", _e)

if board() == "AMYBOARD":
    amyboard.start_amy()
    # Run the self-test if the boot button is now held down.
    import self_test
    self_test.self_test_if_button()

if board() == "AMYBOARD_VCV":
    # In-process AMY was started by the Rack module before MicroPython booted.
    amyboard.start_amy()

if board() == "CYNTHIA":
    # Arm the software watchdog BEFORE start_amy(), because start_amy() runs
    # run_sketch() -> `import sketch`, i.e. the sketch's TOP-LEVEL code, which can
    # hang. The poll (the normal feed) only starts later in attach_current(), so
    # without this an instrument whose import blocks before returning would hang
    # the board permanently with nothing watching — the exact brick this whole
    # mechanism exists to prevent. Disarmed again below if no engine starts, so a
    # headless REPL is not rebooted for being idle.
    try:
        if hasattr(tulip, "cynthia_wdt_arm"):
            # 60s, not the 10s runtime default: a valid boot can legitimately
            # block for seconds (I2C prescan, display init, a sketch that sleeps
            # at import). Shortening this would reboot healthy boards.
            tulip.cynthia_wdt_arm(60000)
    except Exception as _we:
        print("_boot: CYNTHIA could not arm watchdog at boot:", _we)
    amyboard.start_amy()
    # Daughterboard registry + ADS manager come up after AMY is running.
    try:
        from cynthia import registry, ads, config as cynthia_config
        # First boot: seed /user/daughterboard.json with the stock Spark
        # wiring (spark_main + spark_pots_legacy) so the drivers come up.
        # Idempotent — an existing file is never touched.
        cynthia_config.seed_default()
        _cfg = cynthia_config.load()
        ads.setup(chip=_cfg.get("adc_chip", "ads1115"))
        registry.start()
        # amyboardworld compatibility (§17a): give sketches the amyboard.Display
        # surface (128x128 GS4 canvas over the 128x64 panel) and no-op the
        # AMYboard-only hardware helpers, so library sketches run unedited.
        try:
            from cynthia import surface as _surface, compat as _compat
            _sh1106 = _surface.display()
            _compat.install(amyboard, _sh1106)
        except Exception as _ce:
            print("_boot: CYNTHIA compat install failed:", _ce)
        # Attach the runtime to the instrument start_amy() just ran, so
        # pots/buttons/display are routed to its engine (§17 PLAY mode).
        # In safe mode no instrument was restored (and /user/current/sketch.py
        # was reset to the default), so don't claim one is attached.
        from cynthia import runtime as _runtime, browser as _browser
        _runtime.attach_current(None if _skipped else _browser.last_instrument())
        # Apply persisted MIDI settings (channel/velocity/octave) so the saved
        # choices are live before the first key press.
        try:
            from cynthia import settings as _csettings
            _csettings.apply_to_runtime()
        except Exception as _se:
            print("_boot: CYNTHIA settings apply failed:", _se)
        # BLE-MIDI (§16): only when enabled in settings. Started AFTER the
        # instrument is attached so the prompt reflects the real play state; the
        # coroutine runs on the same asyncio loop uasyncio/serial already uses.
        try:
            from cynthia import ble_midi as _ble, settings as _cs2
            if _cs2.get("ble_midi_enabled"):
                # start() is synchronous: the raw bluetooth API advertises and
                # delivers events via IRQ, so no asyncio task is involved.
                if _ble.start(name=_cs2.get("ble_name")):
                    print("_boot: CYNTHIA BLE-MIDI advertising as %r"
                          % _cs2.get("ble_name"))
                else:
                    print("_boot: CYNTHIA BLE-MIDI failed:",
                          _ble.status()["error"])
        except Exception as _be:
            print("_boot: CYNTHIA BLE-MIDI start failed:", _be)
    except Exception as _e:
        print("_boot: CYNTHIA cynthia init failed:", _e)

    # This boot got all the way here (AMY up, instrument restored and its
    # top-level code run, runtime attached), so it counts as a good boot. If the
    # sketch's loop() later starves the watchdog, the reboot will start counting
    # from 1 again — which is what makes the counter a measure of boots that
    # never *completed*, not of crashes in general.
    if _guard is not None and not _skipped:
        try:
            _guard.clear()
        except Exception as _e:
            print("_boot: CYNTHIA guard clear failed:", _e)

    # The watchdog was armed above (before the sketch's top-level code ran). If
    # nothing is going to feed it — no engine attached, or safe mode — disarm, or
    # a quiet headless REPL would look like a hang and reboot forever.
    try:
        from cynthia import runtime as _rt
        if not _rt._running:
            tulip.cynthia_wdt_disarm()
            print("_boot: CYNTHIA no poll running — watchdog disarmed")
    except Exception as _we:
        print("_boot: CYNTHIA watchdog disarm check failed:", _we)

# We don't do most of Tulip's MIDI, but setup the MIDI callback hook so midi.add_callback(fn) will work.
if board() == "AMYBOARD" or board() == "AMYBOARD_WEB" or board() == "AMYBOARD_VCV" or board() == "CYNTHIA":
    tulip.midi_callback(midi.c_fired_midi_event)

if board() != "AMYBOARD" and board() != "AMYBOARD_WEB" and board() != "AMYBOARD_VCV":
    midi.setup() # Just mirrors the setup mostly managed by AMY for Tulip-REPL systems.

