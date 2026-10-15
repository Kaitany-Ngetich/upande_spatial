def classFactory(iface):  # noqa: N802 - QGIS plugin entry point name
    from .plugin import UpandeSpatialPlugin

    return UpandeSpatialPlugin(iface)
