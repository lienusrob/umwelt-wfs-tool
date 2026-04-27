def classFactory(iface):
    import sys
    # Flush all cached submodules so code changes take effect on plugin reload
    stale = [k for k in list(sys.modules) if k.startswith(__name__ + ".")]
    for k in stale:
        del sys.modules[k]

    from .umwelt_plugin_bfn import UmweltPluginBFN
    return UmweltPluginBFN(iface)
