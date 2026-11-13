# sqlite-fts4

[![PyPI](https://img.shields.io/pypi/v/sqlite-fts4.svg)](https://pypi.org/project/sqlite-fts4/)
[![Changelog](https://img.shields.io/github/v/release/simonw/sqlite-fts4?include_prereleases&label=changelog)](https://github.com/simonw/sqlite-fts4/releases)
[![Tests](https://github.com/simonw/sqlite-fts4/workflows/Test/badge.svg)](https://github.com/simonw/sqlite-fts4/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/simonw/sqlite-fts4/blob/main/LICENSE)

Custom SQLite functions written in Python for ranking documents indexed using the FTS4 extension.

Read [Exploring search relevance algorithms with SQLite](https://simonwillison.net/2019/Jan/7/exploring-search-relevance-algorithms-sqlite/) for further details on this project.

## Demo

You can try out these SQL functions [using this interactive demo](https://datasette-sqlite-fts4.datasette.io/24ways-fts4?sql=select%0D%0A++++json_object%28%0D%0A++++++++"label"%2C+articles.title%2C+"href"%2C+articles.url%0D%0A++++%29+as+article%2C%0D%0A++++articles.author%2C%0D%0A++++rank_score%28matchinfo%28articles_fts%2C+"pcx"%29%29+as+score%2C%0D%0A++++rank_bm25%28matchinfo%28articles_fts%2C+"pcnalx"%29%29+as+bm25%2C%0D%0A++++json_object%28%0D%0A++++++++"pre"%2C+annotate_matchinfo%28matchinfo%28articles_fts%2C+"pcxnalyb"%29%2C+"pcxnalyb"%29%0D%0A++++%29+as+annotated_matchinfo%2C%0D%0A++++matchinfo%28articles_fts%2C+"pcxnalyb"%29+as+matchinfo%2C%0D%0A++++decode_matchinfo%28matchinfo%28articles_fts%2C+"pcxnalyb"%29%29+as+decoded_matchinfo%0D%0Afrom%0D%0A++++articles_fts+join+articles+on+articles.rowid+%3D+articles_fts.rowid%0D%0Awhere%0D%0A++++articles_fts+match+%3Asearch%0D%0Aorder+by+bm25&search=jquery+maps).

## Installation

    pip install sqlite-fts4

## Usage

This module implements several custom SQLite3 functions. You can register them against an existing SQLite connection like so:

```python
import sqlite3
from sqlite_fts4 import register_functions

conn = sqlite3.connect(":memory:")
register_functions(conn)
```

If you only want a subset of the functions registered you can do so like this:

```python
from sqlite_fts4 import rank_score

conn = sqlite3.connect(":memory:")
conn.create_function("rank_score", 1, rank_score)
```

if you want to use these functions with [Datasette](https://github.com/simonw/datasette) you can enable them by installing the [datasette-sqlite-fts4](https://github.com/simonw/datasette-sqlite-fts4) plugin:

    pip install datasette-sqlite-fts4

## rank_score()

This is an extremely simple ranking function, based on [an example](https://www.sqlite.org/fts3.html#appendix_a) in the SQLite documentation. It generates a score for each document using the sum of the score for each column. The score for each column is calculated as the number of search matches in that column divided by the number of search matches for every column in the index - a classic [TF-IDF](https://en.wikipedia.org/wiki/Tf%E2%80%93idf) calculation.

You can use it in a query like this:

```sql
select *, rank_score(matchinfo(docs, "pcx")) as score
from docs where docs match "dog"
order by score desc
```

You *must* use the `"pcx"` matchinfo format string here, or you will get incorrect results.

## rank_bm25()

An implementation of the [Okapi BM25](https://en.wikipedia.org/wiki/Okapi_BM25) scoring algorithm. Use it in a query like this:

```sql
select *, rank_bm25(matchinfo(docs, "pcnalx")) as score
from docs where docs match "dog"
order by score desc
```

You *must* use the `"pcnalx"` matchinfo format string here, or you will get incorrect results. If you see any `math domain` errors in your logs it may be because you did not use exactly the right format string here.

## decode_matchinfo()

SQLite's [built-in matchinfo() function](https://www.sqlite.org/fts3.html#matchinfo) returns results as a binary string. This binary represents a list of 32 bit unsigned integers, but reading the binary results is not particularly human-friendly.

The `decode_matchinfo()` function decodes the binary string and converts it into a JSON list of integers.

Usage:

```sql
select *, decode_matchinfo(matchinfo(docs, "pcx"))
from docs where docs match "dog"
```

Example output:

    hello dog, [1, 1, 1, 1, 1]

## annotate_matchinfo()

This function decodes the matchinfo document into a verbose JSON structure that describes exactly what each of the returned integers actually means.

Full documentation for the different format string options can be found here: https://www.sqlite.org/fts3.html#matchinfo

## Federated ranking across separate FTS4 libraries

A per-library `rank_bm25()` first place is not comparable across libraries:
each FTS4 table computes document frequency, average field length and the
total document count from its own rows only. `sqlite_fts4.federation`
recomputes Okapi BM25 against the *merged* corpus statistics instead:

- the total document count is the sum of the per-library row counts,
- each field's total token length is summed across libraries,
- each term's document frequency is summed per field across libraries.

Wrap each local table in an `FtsLibrary` carrying a `column_map` from local
column names to the shared canonical column names. The canonical column order
in every result is the order of those mapping values.

```python
import sqlite3
from sqlite_fts4 import FtsLibrary, rank_federated

north = sqlite3.connect("north.db")
south = sqlite3.connect("south.db")  # uses different local column names

libraries = [
    FtsLibrary(north, "docs", {"title": "name", "body": "content"}, name="north"),
    FtsLibrary(south, "docs", {"headline": "name", "text": "content"}, name="south"),
]

ranking = rank_federated(libraries, "dog park")
print(ranking.columns)            # ('name', 'content')
for result in ranking:
    print(result.rank, result.score, result.library, result.docid, result.values)
```

Ties share a competition rank (two first places are followed by rank 3); the
stable tie-break is library order then original `rowid`.

### Early stopping

Pass `limit` to keep only the top results. With `early_stop=True` (the
default) the remaining libraries are skipped only once an upper bound on the
best score any unseen document could reach proves it cannot make the current
top `limit`. The bound uses each term's total hit count in the unseen
libraries as the maximum possible single-document term frequency, so a later
library holding the overall champion is never skipped. Use
`ranking.stopped_early` and `ranking.libraries_searched` to inspect what
happened, or pass `early_stop=False` to force every library to be searched.

### Synthetic merged library

For small libraries you can instead copy every document into one synthetic
FTS4 database and query it directly. It registers a ranking function over the
same merged statistics and shares the exact scoring and ranking code, so the
ranks and tie handling are identical to `rank_federated()`.

```python
from sqlite_fts4 import merge_libraries

merged = merge_libraries(libraries)
ranking = merged.search("dog park", limit=5)
```

Both paths accept custom `k1` and `b` parameters (defaults `1.2` and `0.75`,
matching `rank_bm25()`).


You need to call this function with the same format string as was passed to `matchinfo()` - for example:

```sql
select annotate_matchinfo(matchinfo(docs, "pcxnal"), "pcxnal")
from docs where docs match "dog"
```

The returned JSON will include a key for each letter in the format string. For example:

```json
{
    "p": {
        "value": 1,
        "title": "Number of matchable phrases in the query"
    },
    "c": {
        "value": 1,
        "title": "Number of user defined columns in the FTS table"
    },
    "x": {
        "value": [
            {
                "column_index": 0,
                "phrase_index": 0,
                "hits_this_column_this_row": 1,
                "hits_this_column_all_rows": 2,
                "docs_with_hits": 2
            }
        ],
        "title": "Details for each phrase/column combination"
    },
    "n": {
        "value": 3,
        "title": "Number of rows in the FTS4 table"
    },
    "a": {
        "title":"Average number of tokens in the text values stored in each column",
        "value": [
            {
                "column_index": 0,
                "average_num_tokens": 2
            }
        ]
    },
    "l": {
        "title": "Length of value stored in current row of the FTS4 table in tokens for each column",
        "value": [
            {
                "column_index": 0,
                "length_of_value": 2
            }
        ]
    }
}
```
