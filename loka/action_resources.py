"""Worker-only disposal of detached preview CPU owners and shared caches."""
import gc


def release_preview_resources(bundle):
    # No QObject or GL resource belongs in this bundle. Cancelled sound readers
    # must finish before their source models can be destroyed and conversion runs.
    owners, loaders = bundle
    for thread, result in loaders:
        thread.join()
        result.clear()
    loaders.clear()
    owners.clear()
    from .action_materials import clear_texture_cache
    from .action_arms import clear_arms_cache
    clear_texture_cache()
    clear_arms_cache()
    gc.collect()
