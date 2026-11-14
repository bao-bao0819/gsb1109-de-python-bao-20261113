[![Build Status](https://travis-ci.org/tkrajina/srtm.py.svg?branch=master)](https://travis-ci.org/tkrajina/srtm.py)

# SRTM.py

SRTM.py is a python parser for the Shuttle Radar Topography Mission elevation data.

See: [http://www2.jpl.nasa.gov/srtm/](http://www2.jpl.nasa.gov/srtm/).

You can see SRTM.py in action on [Trackprofiler (online GPS track editor and organizer)](http://www.trackprofiler.com).

There is also a Golang port of this library: [go-elevations](https://github.com/tkrajina/go-elevations).

## Usage:

    import srtm
    elevation_data = srtm.get_data()
    print('CGN Airport elevation (meters):', elevation_data.get_elevation(50.8682, 7.1377))

Cache files with urls of all SRTM files are generated in your HOME directory. In case, you need another location:

    import srtm
    elevation_data = srtm.get_data(local_cache_dir="mydir")
    print('CGN Airport elevation (meters):', elevation_data.get_elevation(50.8682, 7.1377))

## GPS Tracks

You can add elevations for all points in a GPS track with:

    import srtm
    import gpxpy
    gpx = gpxpy.parse(open('your-gpx-file.gpx'))
    elevation_data = srtm.get_data()
    elevation_data.add_elevations(gpx)

But this is raw SRTM data. If you need some approximations, you can try with:

    import srtm.gpx
    import gpxpy
    gpx = gpxpy.parse(open('your-gpx-file.gpx'))
    elevation_data = srtm.get_data()
    elevation_data.add_elevations(gpx, smooth=True)

The result on a graph:

![GPX elevations](http://tkrajina.github.io/srtm.py/gpx_elevations.png)

In gray is the original elevation (taken with an Android smartphone).
Blue is raw-srtm-data elevations, in red is the smoothed (approximated) srtm data.

You need [gpxpy](http://github.com/tkrajina/gpxpy) installed in order for this feature to work.

## Depression merge tables

Beyond "how much is flooded at one level", `srtm.basin_merges` answers "at which elevation do two depressions first connect". It builds a persistent, repeatedly queryable merge table from a local `.hgt` grid:

    from srtm.basin_merges import build_from_hgt_file, load_basin_merge_table

    table = build_from_hgt_file('test_files/N44W072.hgt')
    table.save('N44W072_merges.json')   # build once, query many times
    table.save_csv('N44W072_merges.csv')

    reloaded = load_basin_merge_table('N44W072_merges.json')
    reloaded.connection_level(0, 1)     # elevation where depressions 0 and 1 join, or None
    reloaded.basin_for_query([0, 1])    # level, saddle position, members and flooded area

Boundary conventions used by every query:

 * SRTM no-data cells (outside `[-1000, 10000]`) are permanent barriers. They are never flooded and can never connect two depressions, so connectivity is never decided through a void.
 * Connectivity is 4-connected (no diagonal touch); a cell at elevation `h` floods when the water reaches `h`.
 * A saddle that joins three or more depressions at the same height is a single merge event with all of them as members, not several pairwise merges.
 * The grid border is an open spill boundary. A depression reaching the border is recorded as a border spill rather than an inter-depression merge.

The table is independent of cell reading order: shuffling the input cells, or rotating/reflecting the grid, leaves every merge elevation and member set unchanged (only cell coordinates follow the grid).

From the command line:

    python -m srtm.basin_merges test_files/N44W072.hgt --json merges.json --csv merges.csv

## Elevation images

You can create elevation images with:

    import srtm
    geo_elevation_data = srtm.get_data()
    image = geo_elevation_data.get_image((500, 500), (45, 46), (13, 14), 300)
    # the image s a standard PIL object, you can save or show it:
    image.show()

On every elevation requested the library will:

 * Check if the SRTM file is stored locally
 * If not -- download it from NASA servers and store locally (in `~/.cache/srtm/`)
 * Parse elevations from it

That's why the first run of your application will always take a few seconds.

### Example images

Istra and Trieste:

![Istra](http://tkrajina.github.io/srtm.py/istra.png)

Rio de Janeiro:

![Rio](http://tkrajina.github.io/srtm.py/rio.png)

Miami and florida:

![Miami](http://tkrajina.github.io/srtm.py/miami.png)

Sidney:

![Sidney](http://tkrajina.github.io/srtm.py/sidney.png)

## gpxelevations

gpxelevations is a utility command line tool to add/update elevations in a GPS track file:

    $ gpxelevations -h
    usage: gpxelevations [-h] [-o] [-p] [-s] [-c] [-f FILE] [-v]
                         [gpx_files [gpx_files ...]]

    Adds elevation to GPX files

    positional arguments:
      gpx_files             GPX files

    optional arguments:
      -h, --help            show this help message and exit
      -o, --overwrite       Overwrite existing elevations (otherwise will add
                            elevations only where not yet present)
      -p, --approximate     Approximate elevations with neighbour points elevation
      -s, --smooth          Smooth elevations
      -c, --calculate       Calculate elevations (but don't change the GPX file)
      -f FILE, --file FILE  Output filename
      -v, --verbose         Verbose output

## License

SRTM.py is licensed under the [Apache License, Version 2.0](http://www.apache.org/licenses/LICENSE-2.0)

