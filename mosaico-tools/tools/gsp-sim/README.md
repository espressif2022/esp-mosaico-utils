# GSP application preview

From a workspace use `python mosaico.py project sim --project projects/my_app`.
The project can also be selected from cwd, a configured default, or the sole app.
There is no hard-coded Hello World fallback. Install the application's pinned
ESP-GSP 1.4.0 component through an IDF build/reconfigure before native preview.

The runner selects sim_bridge when pc/CMakeLists.txt exists. It compiles shared
C UI logic into the native backend; the matching official simulator renders it.
Use --interactive for browser preview, or --headless --duration 3 for a bounded
smoke run. Backend build artifacts are local to the application in build-sim/.

--scene-only, --frames, --fps and --dump-ppm select scene-only rendering.
These modes do not execute application C callbacks. The public product command
accepts --dump-ppm PATH; direct `run.py --project PATH` or an explicit scene file
is also supported. Tool downloads use user caches and do not rely on another app.
