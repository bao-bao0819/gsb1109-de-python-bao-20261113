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

## Inverse distance weighted interpolation with barriers

`get_elevation` returns the raw nearest grid cell. For sub-cell locations the
library also offers inverse distance weighted (IDW) interpolation through
`_IDW`. When the elevation grid contains voids (for example around a dam or a
reservoir), a sample on the other side of the dam may be geometrically close
but physically unrelated. Optional **barrier lines** and **sampling valid
regions** prevent such samples from contributing:

    import srtm
    # A barrier is a (latitude, longitude) polyline, or a list of polylines.
    # Coordinates are global, so one barrier may span several SRTM files.
    dam_barrier = [(44.18, -71.60), (44.17, -71.59)]
    elevation_data = srtm.get_data(barriers=dam_barrier)
    elevation = elevation_data._IDW(44.1756325, -71.5965699)

A support point contributes only when the straight path from the query point
to it does not cross a barrier. The boundary rules are explicit:

 * a support point lying on a barrier, including at a barrier endpoint, is excluded,
 * a proper transversal crossing is excluded,
 * a collinear overlap with the barrier is excluded,
 * a path that merely grazes a barrier endpoint is allowed, and
 * when the query point itself sits on the barrier, paths departing from it are
   allowed unless one of the rules above applies.

Barriers can also be configured or replaced after construction with
`elevation_data.set_barriers(...)`, and individual queries can override the
configured barriers (or valid regions) with their own arguments. Pass `None`
or an empty list to remove the restriction.

**Valid regions** restrict which grid samples are eligible at all:

    valid_region = [[(44.18, -71.60), (44.18, -71.59), (44.17, -71.59), (44.17, -71.60)]]
    elevation_data = srtm.get_data(valid_regions=valid_region)

Use `_IDW_detailed` to get an `IDWResult` instead of a bare float. When the
remaining valid support is insufficient, its `elevation` is `None` (missing
data) and `excluded` lists every rejected candidate, nearest first, with the
reason (`void`, `outside_valid_region`, or
`blocked_by_barrier[i].segment[j]`) and its distance:

    result = elevation_data._IDW_detailed(44.1756325, -71.5965699)
    if result.elevation is None:
        for rejected in result.excluded:
            print(rejected.latitude, rejected.longitude, rejected.distance, rejected.reason)

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

