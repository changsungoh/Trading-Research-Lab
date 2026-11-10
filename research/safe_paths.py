"""Read-only alias checks for report input/output paths, including hard links."""
from pathlib import Path


def aliases(first, second):
    first, second = Path(first).resolve(), Path(second).resolve()
    if first == second:
        return True
    return first.exists() and second.exists() and first.samefile(second)


def unsafe_outputs(sources, outputs):
    return (any(aliases(output, source) for output in outputs for source in sources)
            or any(aliases(output, previous) for index, output in enumerate(outputs)
                   for previous in outputs[:index]))
