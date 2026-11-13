TextFSM
=======

Python module which implements a template based state machine for parsing
semi-formatted text. Originally developed to allow programmatic access to
information returned from the command line interface (CLI) of networking
devices.

The engine takes two inputs - a template file, and text input (such as command
responses from the CLI of a device) and returns a list of records that contains
the data parsed from the text.

A template file is needed for each uniquely structured text input. Some examples
are provided with the code and users are encouraged to develop their own.

By developing a pool of template files, scripts can call TextFSM to parse useful
information from a variety of sources. It is also possible to use different
templates on the same data in order to create different tables (or views).

TextFSM was developed internally at Google and released under the Apache 2.0
licence for the benefit of the wider community.

[**See documentation for more details.**](https://github.com/google/textfsm/wiki/TextFSM)

Offline verification
--------------------
`textfsm-audit` cross-checks a captured CLI echo against the index and its
templates without changing normal parsing behaviour. It is useful when device
identification is incomplete or when more than one template can read the same
echo.

```sh
textfsm-audit examples/cisco_version_example \
  --index index --template-dir examples \
  --attribute Vendor=Cisco \
  --attribute Command='show version'
```

The report separates:

* `determined columns`: every compared template declares the same logical
  field, and (when records exist) the parsed values agree.
* `template-dependent columns`: the field is missing from at least one
  template or its parsed values disagree. These columns should not be treated
  as device facts until the correct template is chosen.

An index match is never assumed to be unique. All matching rows are reported;
the row that the existing first-match behaviour selects is marked as `selected`
so a truncated model value that prefix-matches several rows is visible.

A zero-record result is only reported as `confirmed-empty` when the echo
actually hits the template's own explicit `End` or `EOF` transition. Declaring
an empty `EOF` state in the template is not evidence that it was reached.
Without that in-band terminator the result is reported as
`unverified-missing-end-marker` (or `unverified-template-has-no-end-marker`),
because a truncated or unrecognised echo can also produce zero rows.

If two templates use different names for the same field, supply the field
equivalence map explicitly:

```sh
textfsm-audit echo.txt --template-dir examples \
  --template cisco_version_template:f10_version_template \
  --field-map Model=Chassis \
  --json
```

The same behaviour is available from Python:

```python
from textfsm import audit

report = audit.audit_echo(
    echo,
    index_file='index',
    template_dir='examples',
    attributes={'Vendor': 'Cisco', 'Command': 'show version'},
    field_map={'Model': 'Chassis'},
)
print(report.format_text())
```

The command exits non-zero with `--fail-on-ambiguous-index`,
`--fail-on-unverified`, or `--fail-on-incomplete` when the corresponding
condition is found.

Before contributing
-------------------
If you are not a Google employee, our lawyers insist that you sign a Contributor
Licence Agreement (CLA).

If you are an individual writing original source code and you're sure you own
the intellectual property, then you'll need to sign an
[individual CLA](https://cla.developers.google.com/about/google-individual).
Individual CLAs can be signed electronically. If you work for a company that
wants to allow you to contribute your work, then you'll need to sign a
[corporate CLA](https://cla.developers.google.com/clas).
The Google CLA is based on Apache's. Note that unlike some projects
(notably GNU projects), we do not require a transfer of copyright. You still own
the patch.

Sadly, even the smallest patch needs a CLA.
