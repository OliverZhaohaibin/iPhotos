# MapLibre Demo Tiles

You can see the live demos at the following links. You can also access the styles as JSON for testing in mobile or other style tools.

| Style | Live Demo | `style.json`
| :--- | :--- | :---
| [MapLibre World](#maplibre-world-demo-map) | https://demotiles.maplibre.org | https://demotiles.maplibre.org/style.json
| [OpenMapTiles](tiles-omt),</br>centered around Innsbruck, Austria | https://demotiles.maplibre.org/tiles-omt | https://demotiles.maplibre.org/styles/osm-bright-gl-style/style.json
| [Terrain](terrain-tiles),</br>centered around Innsbruck, Austria | https://demotiles.maplibre.org/terrain-tiles | https://demotiles.maplibre.org/styles/osm-bright-gl-terrain/style.json
| [Debug](debug-tiles),</br>demonstrating tile zoom variation | https://demotiles.maplibre.org/debug-tiles | https://demotiles.maplibre.org/debug-tiles/style.json

## MapLibre World demo map

This is the sample vector map displayed on the frontpage of https://maplibre.org - used in the helloworld examples and CI tests of @MapLibre organization.

It demonstrates the usage of simple vector tiles with the *MapLibre World* map style.

Hosted as static files directly on GitHub Pages, serverless, no keys, runs offline as well.

The MBTiles can be downloaded in the [releases](https://github.com/maplibre/demotiles/releases).
For offline use you can download the [.zip](https://github.com/maplibre/demotiles/archive/refs/heads/gh-pages.zip) including the font and viewer.

![maplibre-world-map-style](https://user-images.githubusercontent.com/59284/118267966-117aa100-b4bd-11eb-8824-430cbe674191.png)

## World countries vector tiles

The map uses a lightweight vector tileset to color and label the world countries. Country polygons are from [Natural Earth Data](https://www.naturalearthdata.com/).
The shapefiles were converted into vector tiles using the [MapTiler Desktop](https://www.maptiler.com/) software, which generates similar .pbf tile directory structure as present, as well as GeoPackage MVT or MBTiles file output.

![maptiler-desktop-generate-vector-tiles](https://user-images.githubusercontent.com/59284/118269170-ac27af80-b4be-11eb-93c9-188c578b914e.gif)

The resulting maplibre.mbtiles is available from this repo (pbf, z0-6, 4Mb).

## Maplibre World map style

The [style.json](style.json) map style renders groups of countries by color using a fill-color match expression on the country layer. Eight colors are taken from [MapLibre Logo / Visuals](https://www.figma.com/file/hVmulAVC8dMwYS14ovGd3w/MapLibre-Logo-%2F-Visuals) defined as follows:

![color-palette](https://user-images.githubusercontent.com/59284/118269371-f3ae3b80-b4be-11eb-8309-1129c1161e30.png)

Design is heavily inspired by the the Geography Class map style from [klokantech/vector-tiles-sample](https://github.com/klokantech/vector-tiles-sample) converted from the original open-source Tilemill style.

```
[
  "match",
  ["get", "ADM0_A3"],
  [
    "ARM",
    "ATG",
    "AUS",
...
],
  "#D6C7FF",
  [...
```

The map labels are using the Open Sans SemiBold font.

## Maplibre Debug Tiles

The [number](debug-tiles/number) tiles contain a black number indicating the tile's zoom level on a colored background, with a black border. These tiles can be used to investigate which zoom levels are loaded.

![0](debug-tiles/number/0.png)![22](debug-tiles/number/22.png)

The [number-hillshade](debug-tiles/number-hillshade) tiles render the zoom level as an elevation that displays the number when used as a hillshade.

The [terrain-ruffles](debug-tiles/terrain-ruffles) tiles contain a ruffle around the border, which helps visualize bouundaries between loaded raster tiles.

## Contributors

the [MapLibre World](#maplibre-world-demo-map) demo was kindly provided by the [MapTiler](https://www.maptiler.com/) team ([@klokan](https://github.com/klokan), [@nbozon](https://github.com/nbozon), [@petr-pokorny-1](https://github.com/petr-pokorny-1), [@tomasklanica](https://github.com/tomasklanica)). 

the [Terrain](terrain-tiles) and [OpenMapTiles](tiles-omt) demos were provided by [@acalcutt](https://github.com/acalcutt) with styles based on [OSM Bright](https://github.com/openmaptiles/osm-bright-gl-style).

The font PBFs were generated using the scripts and source fonts from https://github.com/openmaptiles/fonts.

The debug tiles were provided by [@NathanMOlson](https://github.com/NathanMOlson).
