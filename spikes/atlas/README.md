# Catalog renderer spike (#261)

`render_catalog.py` turns `_atlas/atlas.json` into one self-contained `catalog.html` (list-first catalog with cross-filter facets and a month-by-channel grid; no network resources, opens from disk).
Build: `python spikes/atlas/render_catalog.py --atlas <output_dir>/_atlas/atlas.json` (writes `catalog.html` next to the atlas; `--out` to choose another file). `catalog.html` in this folder is the template.
Test: `pytest spikes/atlas -q -p no:cacheprovider`.
This is a bake-off candidate for issue #261, not the chosen renderer; the output is private, like `_atlas/` itself.
