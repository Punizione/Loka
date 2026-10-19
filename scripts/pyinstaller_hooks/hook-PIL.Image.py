"""Keep Pillow's direct in-memory BCN decoder without file format plugins."""
# Override PyInstaller's stock hook, which collects every ImagePlugin. Analysis
# global excludes in Loka.spec bound all import edges, including ImageFile's
# TIFF edge; excludedimports here would affect only Image's direct edges.
hiddenimports = ['PIL._imaging']
