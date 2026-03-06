import os
import zipfile
import pathlib
import logging
# from importlib.abc import Traversable # since python 3.11

logger = logging.getLogger('restim.funscript')


def case_insensitive_compare(a, b):
    return a.lower() == b.lower()


def split_funscript_path(path):
    a, b = os.path.split(path)
    parts = b.split('.')
    extension = parts[-1]
    if len(parts) == 1:
        return parts[0], '', ''
    if len(parts) == 2:
        return parts[0], '', extension
    return '.'.join(parts[:-2]), parts[-2], extension


class Resource:
    def __init__(self, path):
        self.path = path  # Traversable, since python 3.11

    def open(self, *args, **kwargs):
        return self.path.open(*args, **kwargs)

    def is_funscript(self):
        return case_insensitive_compare(self.path.suffix, '.funscript')

    def funscript_type(self):
        try:
            return self.path.suffixes[-2][1:].lower()
        except IndexError:
            return ''

    def name(self):
        return self.path.name

    def __str__(self):
        return str(self.path)

    def __repr__(self):
        return self.path.__repr__()


def collect_funscripts(
        dirs: list[str],
        media: str
) -> list[Resource]:
    """
    Search the directories in order for funscripts. Stop searching when at least one funscript is found in a directory.
    If a directory is found with the same name as the media, search that directory too.
    zipfiles are supported.
    :param dirs:
    :param media:
    :return:
    """
    grouped = collect_funscripts_grouped(dirs, media)
    # flatten for backward compatibility
    result = []
    for _dir, resources in grouped:
        result.extend(resources)
    return result


def collect_funscripts_grouped(
        dirs: list[str],
        media: str
) -> list[tuple[str, list[Resource]]]:
    """
    Search all directories for funscripts, returning results grouped by source directory.
    Each entry is (directory_display_name, [Resource, ...]).
    Directories are searched in order; results from earlier directories take priority.
    :param dirs:
    :param media:
    :return:
    """
    def path_is_zip(path):
        try:
            zipfile.Path(path)
            return True
        except OSError:
            return False

    def process_dir(start_dir, media_prefix):
        """Search a single top-level search path and return results grouped by actual directory."""
        dir_stack = [start_dir]
        new_dirs = []
        # Collect (actual_directory, Resource) pairs
        collected = []

        while dir_stack:
            try:
                current_dir = os.path.expanduser(dir_stack[0])
                del dir_stack[0]

                logger.info(f'detecting funscripts from {current_dir}')

                if current_dir[-2:]=="/*":
                    current_dir=current_dir[:-2]
                    search_subdirectories=True
                else:
                    search_subdirectories = False

                try:
                    traversing_a_zip = True
                    traversable = zipfile.Path(current_dir)
                except OSError:
                    traversing_a_zip = False
                    traversable = pathlib.Path(current_dir)

                files_before = len(collected)
                media_prefix_dirs = []

                for node in traversable.iterdir():
                    full_path = os.path.join(current_dir, node.name)
                    if not traversing_a_zip and node.is_dir(): # do not support dir-in-zip
                        if case_insensitive_compare(node.name, media_prefix):
                            media_prefix_dirs.append(full_path)
                        elif search_subdirectories:
                            new_dirs.append(full_path+"/*")
                    else:
                        a, b, c = split_funscript_path(full_path)
                        if case_insensitive_compare(a, media_prefix):
                            if not traversing_a_zip and zipfile.is_zipfile(full_path):    # do not support zip-in-zip
                                new_dirs.append(full_path)
                            elif case_insensitive_compare(c, 'funscript'):
                                collected.append((current_dir, Resource(node)))

                # Only search media-prefix subdirs as fallback when
                # this directory yielded no scripts itself.
                if len(collected) == files_before:
                    new_dirs.extend(media_prefix_dirs)

            except OSError as e:    # unreachable network?
                pass

            # make sure to search dirs before zipfiles
            new_zips = list(filter(path_is_zip, new_dirs))
            new_dirs = list(filter(lambda x: not path_is_zip(x), new_dirs))
            dir_stack = new_dirs + new_zips + dir_stack
            new_dirs = []

        # Group by actual directory, preserving discovery order
        groups = {}
        group_order = []
        for dir_path, resource in collected:
            if dir_path not in groups:
                groups[dir_path] = []
                group_order.append(dir_path)
            groups[dir_path].append(resource)
        return [(d, groups[d]) for d in group_order]

    media_prefix, _, media_extension = split_funscript_path(media)

    grouped_results = []
    seen_paths = set()
    for search_dir in dirs:
        sub_groups = process_dir(search_dir, media_prefix)
        for dir_name, resources in sub_groups:
            # deduplicate: skip files already found in an earlier batch
            unique = []
            for r in resources:
                norm = os.path.normcase(os.path.normpath(str(r.path)))
                if norm not in seen_paths:
                    seen_paths.add(norm)
                    unique.append(r)
            if unique:
                grouped_results.append((dir_name, unique))

    return grouped_results
