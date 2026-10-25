import copy
import json
import re


def apply(sent, previous):
    full = re.search(r'(?m)^实时上下文：(.*)', sent)
    if full:
        return json.JSONDecoder().raw_decode(full.group(1))[0]
    changes = json.JSONDecoder().raw_decode(sent.split('上下文增量：', 1)[1])[0]['changes']
    result = copy.deepcopy(previous)
    for change in changes:
        path = change.get('set', change.get('remove'))
        parent = result
        for part in path[:-1]:
            parent = parent[part]
        if 'remove' in change:
            del parent[path[-1]]
        elif not path:
            result = change['value']
        else:
            parent[path[-1]] = change['value']
    return result
