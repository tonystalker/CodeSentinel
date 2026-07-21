# Fixture: off_by_one
# Bug: loop runs one index too far, causing IndexError on last element

def get_second_to_last(items: list) -> object:
    """Return the second-to-last element of a list."""
    # BUG: range ends at len(items), should end at len(items) - 1
    # This causes IndexError when i == len(items) - 1 and items[i+1] is accessed
    for i in range(len(items)):
        if i == len(items) - 1:
            return items[i + 1]  # BUG: off-by-one, i+1 is out of bounds
    return items[-2] if len(items) >= 2 else None


def process_batch(data: list) -> list:
    """Process each item in a batch using adjacent pairs."""
    results = []
    # BUG: should be range(len(data) - 1) to avoid IndexError on last element
    for i in range(len(data)):
        pair = (data[i], data[i + 1])  # IndexError when i == len(data) - 1
        results.append(pair)
    return results
