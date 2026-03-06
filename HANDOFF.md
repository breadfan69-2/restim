# Funscript Search Improvements — Handoff Doc

## Problem
When funscripts lived in different folders than the media file, REstim wouldn't find them unless the user manually configured additional search paths. Even with additional search paths, the search stopped at the **first folder** that contained any match — so scripts split across multiple folders were never fully discovered.

Additionally, the "recursive search" checkbox in the Additional Search Paths dialog was not labeled or explained, making it easy to miss.

## Research Summary

### How funscript discovery worked (before changes)
1. External media player reports the loaded file path (e.g. `C:\Videos\myvideo.mp4`)
2. `media_settings_widget.py` builds a search path list: `[media_dir] + additional_search_paths`
3. `collect_funscripts()` iterates the search path list with a **single shared queue** and a `while dir_stack and len(collected_files) == 0` loop — **stops at the first directory with any match**
4. All found scripts go into a single flat "Funscripts (auto-detected)" tree node

### Key files
| File | Role |
|------|------|
| `funscript/collect_funscripts.py` | Filesystem search logic |
| `qt_ui/models/script_mapping.py` | Tree model for the funscript list UI |
| `qt_ui/media_settings_widget.py` | Media tab widget, triggers search |
| `qt_ui/models/additional_search_paths.py` | Search paths dialog model (checkbox = recursive) |
| `qt_ui/widgets/table_view_with_combobox.py` | Delegates for combobox + trash button columns |

### Recursive search (upstream commit c5754234)
Already present in v1.56. In the Additional Search Paths dialog, checking the checkbox next to a path enables recursive subdirectory searching. Stored as `path/*` suffix in settings.

### UI indicators in the script tree
- **Red text** = broken/unparseable funscript JSON
- **Greyed out** = duplicate axis assignment (`first_of_its_kind` is false)
- **Trash icon** = manually added script (removable)

## Changes Made

### 1. `funscript/collect_funscripts.py`
- Added `collect_funscripts_grouped()` — searches **every** directory independently and returns `list[tuple[str, list[Resource]]]` (folder path + resources found in it)
- Original `collect_funscripts()` kept as backward-compatible wrapper that flattens the grouped result
- Each top-level search path is processed independently via `process_dir()`, so finding scripts in one folder no longer prevents searching other folders

### 2. `qt_ui/models/script_mapping.py`
- Replaced single `_funscripts_auto` category with `_funscripts_auto_categories: list[ResourceCategory]`
- Each folder that returns results gets its own `ResourceCategory` node in the tree, labeled with the folder path
- `_all_auto_children()` helper iterates all auto categories for `funscript_conifg()`, `get_config_for_axis()`, `refresh_active_files()`
- `detect_funscripts_from_path()` now calls `collect_funscripts_grouped()` and creates per-folder categories
- `clear_auto_detected_funscripts()` removes all auto categories from the root

### Result in the UI
```
Funscripts (manual)
C:\Videos
  └── myvideo.alpha.funscript     → alpha
C:\Funscripts\subfolder
  └── myvideo.beta.funscript      → beta
```

Duplicates across folders (same axis) are greyed out — earlier folders take priority.

## Testing
- Import smoke test: both modified modules import cleanly
- Functional test: created temp dirs with scripts split across two folders, `collect_funscripts_grouped()` correctly returned both groups (previously only the first was found)
