import sys, time
import os as _os; REPO = _os.environ.get("DEWEY_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
sys.argv=[sys.argv[0], sys.argv[1]]
exec(open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 't01_console.py')).read().split("s, j = call(\"GET\", \"/api/runner\"); check(\"runner available")[0])
for i in range(2):
    s,j=call("POST","/api/runner/arm",{"code":"111111"}); check("wrong code %d"%i, s==400 and "locked" not in j["error"], (s,j))
s,j=call("POST","/api/runner/arm",{"code":"111111"}); check("third wrong code locks", s==400 and "locked" in j["error"], (s,j))
s,j=call("POST","/api/runner/arm",{"code":code()}); check("right code refused while locked", s==400 and "locked" in j["error"], (s,j))
time.sleep(1.5); s,j=call("GET","/api/runner"); check("lock shown to the page", j["locked_for"]>500, j)
print("FAILS",fails)
