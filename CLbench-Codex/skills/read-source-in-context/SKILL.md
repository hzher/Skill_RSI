---
name: read-source-in-context
description: Locate relevant implementation details when reading large public source files through a shell that limits the size of each observation.
---

If an implementation file or a combined source dump is likely to exceed the shell's output limit, start with a symbol from the public issue or a path already observed. Search the repository for that symbol and read the matching definition with a small amount of surrounding code. File names and line numbers make it easier to return to the same section. Keep enough room in the same tool response for each requested section.

When the shell reports an omitted middle section, use the returned matches to request the missing range. Expand the view if a caller, neighboring branch, or public test is needed to understand the behavior. A short, complete file can be read directly. Continue to inspect edited code and run the checks needed for the repair.
