"""Does the generated schema describe the parameters TRUTHFULLY?

The bug this covers: every inferred parameter was declared `"type": "string"`,
hardcoded. `params.get("seconds", 0)` therefore produced

    {"type": "string", "default": "0"}

— a string whose default is the *string* "0". The schema is not decoration: it
is sent verbatim as the function-calling `parameters` (OpenAI/DeepSeek) and as
`input_schema` (Claude), so it told the model to send "3661" to a handler doing
`int(seconds)`. `normalise` also reads the declared type to coerce inputs.

Each case below is a shape a model actually writes. The assertion is on the
type and default, not on the whole dict, so a description change cannot fail it.
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\Kunoir\Codeground\Clicky\Addled")

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


from backend.skills.forge import SkillForge

forge = SkillForge()


def props(code):
    return forge._infer_params(code, "a task").get("properties", {})


print("=== a numeric default is a number, not a string ===")
# The exact shape from the live forge.
p = props('''
async def f(params: dict) -> dict:
    seconds = params.get("seconds", 0)
    return {"success": True}
''')
check("integer default -> type integer", p.get("seconds", {}).get("type") == "integer",
      str(p.get("seconds")))
check("and the default is the NUMBER 0, not the string '0'",
      p.get("seconds", {}).get("default") == 0
      and not isinstance(p.get("seconds", {}).get("default"), str),
      repr(p.get("seconds", {}).get("default")))

print()
print("=== other literal defaults ===")
p = props('''
async def f(params: dict) -> dict:
    text = params.get("text", "hello")
    ratio = params.get("ratio", 1.5)
    flag = params.get("flag", True)
    return {"success": True}
''')
check("string default -> string", p.get("text", {}).get("type") == "string",
      str(p.get("text")))
check("float default -> number", p.get("ratio", {}).get("type") == "number",
      str(p.get("ratio")))
check("bool default -> boolean", p.get("flag", {}).get("type") == "boolean",
      str(p.get("flag")))
# A bool is an int in Python: this is the trap in `_type_of`.
check("a bool default is NOT reported as integer",
      p.get("flag", {}).get("type") != "integer", str(p.get("flag")))

print()
print("=== no default: the type comes from how it is USED ===")
p = props('''
async def f(params: dict) -> dict:
    n = params.get("count")
    total = int(n) + 1
    return {"success": True, "total": total}
''')
check("int(x) with no default -> number/integer",
      p.get("count", {}).get("type") in ("number", "integer"),
      str(p.get("count")))

p = props('''
async def f(params: dict) -> dict:
    items = params.get("sources")
    return {"success": True, "n": len(items)}
''')
check("len(x) with no default -> array", p.get("sources", {}).get("type") == "array",
      str(p.get("sources")))
check("an array declares items, so normalise can coerce a scalar",
      isinstance(p.get("sources", {}).get("items"), dict),
      str(p.get("sources")))

p = props('''
async def f(params: dict) -> dict:
    verbose = params.get("verbose")
    if verbose == True:
        return {"success": True}
    return {"success": True}
''')
check("a comparison with True -> boolean",
      p.get("verbose", {}).get("type") == "boolean", str(p.get("verbose")))

print()
print("=== a default of None is not mistaken for a string ===")
p = props('''
async def f(params: dict) -> dict:
    x = params.get("maybe", None)
    return {"success": True}
''')
check("None default does not become the string 'None'",
      p.get("maybe", {}).get("default") is None
      and p.get("maybe", {}).get("type") != "string",
      str(p.get("maybe")))

print()
print("=== the OTHER ways a handler reads params ===")
# The forge prompt says "Accept a `params` dict". A model may read it with
# `params["x"]` or guard with `"x" not in params` — both correct, and NEITHER was
# recognised, so such a skill got the dummy query/input fallback while its real
# parameter went undeclared. Found from a locally forged skill that did this.
p = props('''
async def f(params: dict) -> dict:
    if 'seconds' not in params:
        return {'success': False}
    seconds = params['seconds']
    return {'success': True, 'seconds': seconds}
''')
check("a subscript read declares the parameter", "seconds" in p, str(p))
check("no dummy query/input fallback", "query" not in p and "input" not in p,
      str(list(p)))
check("the guard supplies no default, so no default is claimed",
      "default" not in p.get("seconds", {}), str(p.get("seconds")))

p = props('''
async def f(params: dict) -> dict:
    url = params["url"]
    timeout = params.get("timeout", 30)
    return {"success": True}
''')
check("both access styles are found", {"url", "timeout"} <= set(p), str(list(p)))
check("the number default is still inferred from .get",
      p.get("timeout", {}).get("type") in ("integer", "number"),
      str(p.get("timeout")))

p = props('''
async def f(params: dict) -> dict:
    if "path" in params:
        return {"success": True, "p": params["path"]}
    return {"success": False}
''')
check("an `in params` check alone still declares the parameter",
      "path" in p, str(list(p)))

print()
print("=== a subscript value's type comes from its use ===")
p = props('''
async def f(params: dict) -> dict:
    n = params['count']
    return {"success": True, "n": int(n) + 1}
''')
check("int(x) on a subscript read -> number/integer",
      p.get("count", {}).get("type") in ("integer", "number"), str(p.get("count")))

print()
print("=== nothing is declared `required` ===")
# `normalise` ENFORCES required, so inferring it could make a working skill
# refuse a call whose handler copes without the key.
code = '''
async def f(params: dict) -> dict:
    a = params.get("a")
    b = params.get("b", 1)
    return {"success": True}
'''
check("required stays empty even when a parameter has no default",
      forge._infer_params(code, "t").get("required") == [],
      str(forge._infer_params(code, "t").get("required")))

print()
print("=== and it does not crash on a shape it cannot read ===")
p = props('''
async def f(params: dict) -> dict:
    v = params.get(name_from_a_variable)
    return {"success": True}
''')
check("a non-literal key is skipped, not guessed",
      "name_from_a_variable" not in p, str(p))
p2 = props("this is not python at all (")
check("unparseable code falls back, rather than raising", isinstance(p2, dict),
      str(p2)[:80])

print()
print("FAILED: " + ", ".join(fails) if fails
      else "all parameter-inference checks passed")
raise SystemExit(1 if fails else 0)
