from os import environ as env
import multiprocessing

# Port to bind to
bind = f":{int(env.get('PORT', 5003))}"

# Number of processes to launch
workers = int(env.get('WORKERS', multiprocessing.cpu_count()))

# Number of concurrent handled connections
threads = int(env.get('THREADS', 4))
worker_connections = int(env.get('WORKER_CONNECTIONS', '1000'))

# Recycle the process after X request randomized by the jitter
max_requests = int(env.get('MAX_REQUESTS', '1000'))
max_requests_jitter = int(env.get('MAX_REQUESTS_JITTER', '100'))

# Connection timeouts
#  - Defaults to double what the poll length for services should be
graceful_timeout = int(env.get('GRACEFUL_TIMEOUT', '60'))
timeout = int(env.get('TIMEOUT', '60'))
