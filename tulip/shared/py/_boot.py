import gc
import uos
print("_boot: importing modules...")
import tulip, sys, midi, synth, amy, world, sequencer
print("_boot: modules imported")
from upysh import *
from tulip import board
print("_boot: board =", board())

if board()!="AMYBOARD" and board()!="AMYBOARD_WEB" and board()!="AMYBOARD_VCV" and board()!="CYNTHIA":
    from tulip import edit, run
else:
    if board()=="AMYBOARD":
        import amyboard
        amyboard.mount_sd()
        edit = amyboard.edit
    elif board()=="CYNTHIA":
        print("_boot: importing amyboard...")
        import amyboard
        print("_boot: amyboard imported")
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
    print("_boot: importing Partition...")
    from esp32 import Partition
    print("_boot: finding partitions...")
    try:
        s = Partition.find(Partition.TYPE_DATA, label="system")[0]
        u = Partition.find(Partition.TYPE_DATA, label="vfs")[0]
        print("_boot: partitions found")
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
        print("_boot: partitions mounted")
        
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
    print("_boot: CYNTHIA ensure_user_environment...")
    amyboard.ensure_user_environment()
    print("_boot: CYNTHIA ensure_user_environment done")
    # CYNTHIA is headless - skip init_display()
    # CYNTHIA boot flow (§17): restore the last-selected instrument's
    # sketch.py into /user/current/ BEFORE start_amy() runs it. The
    # interactive browser is a REPL command (cynthia.browser.run()) —
    # boot must not block on input() headless.
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
    print("_boot: CYNTHIA start_amy...")
    amyboard.start_amy()
    print("_boot: CYNTHIA start_amy done")
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
        # Attach the runtime to the instrument start_amy() just ran, so
        # pots/buttons/display are routed to its engine (§17 PLAY mode).
        from cynthia import runtime as _runtime, browser as _browser
        _runtime.attach_current(_browser.last_instrument())
    except Exception as _e:
        print("_boot: CYNTHIA cynthia init failed:", _e)

# We don't do most of Tulip's MIDI, but setup the MIDI callback hook so midi.add_callback(fn) will work.
if board() == "AMYBOARD" or board() == "AMYBOARD_WEB" or board() == "AMYBOARD_VCV" or board() == "CYNTHIA":
    print("_boot: setting up MIDI callback...")
    tulip.midi_callback(midi.c_fired_midi_event)
    print("_boot: MIDI callback done")

if board() != "AMYBOARD" and board() != "AMYBOARD_WEB" and board() != "AMYBOARD_VCV":
    print("_boot: midi.setup()...")
    midi.setup() # Just mirrors the setup mostly managed by AMY for Tulip-REPL systems.
    print("_boot: midi.setup() done")

print("_boot: complete!")
