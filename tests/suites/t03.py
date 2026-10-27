import sys, time
import os as _os; REPO = _os.environ.get("DEWEY_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
exec(open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 't01_console.py')).read().split("s, j = call(\"GET\", \"/api/runner\"); check(\"runner available")[0])
s,j=call("POST","/api/runner/arm",{"code":code()}); check("armed for 1 minute", s==200 and 0<j["armed_left"]<=60, (s,j))
s,j=start_dry(); d=j["id"]; wait_job(d)
time.sleep(62)
s,j=call("GET","/api/runner"); check("arming expired by itself", not j["armed"], j)
s,j=real(d); check("real run refused after expiry", s==400 and "not armed" in j["error"], (s,j))
print("FAILS",fails)
