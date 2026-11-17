# Docker lifecycle evidence

## Reading container events
'kill' signal 15 followed seconds later by 'kill' signal 9, then 'stop' and 'die' exit 137, is the signature
of docker stop with a timeout (graceful stop, then forced). An OOM kill shows an 'oom' event and
oom_killed=true instead. Exit code 0 is a clean exit by the process itself. A container in 'restarting'
repeatedly is crash-looping under its restart policy: read its logs for the error that repeats.
