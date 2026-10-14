;;;; scripts/daemon.lisp
;;;;
;;;; Shared cl-mcp daemon entry point.
;;;;
;;;; Load this after cl-mcp itself is loaded, for example:
;;;;
;;;;   sbcl --non-interactive \
;;;;        --load ~/quicklisp/setup.lisp \
;;;;        --eval '(ql:quickload :cl-mcp :silent t)' \
;;;;        --load scripts/daemon.lisp
;;;;
;;;; It starts the multi-client TCP transport with the worker pool enabled
;;;; (the default; MCP_NO_WORKER_POOL must NOT be set) and then sleeps
;;;; forever.  Each TCP connection is its own MCP session with its own
;;;; worker and its own project root (from the client's initialize
;;;; rootPath/rootUri or fs-set-project-root), so many agents -- including
;;;; agents running in different git worktrees of one repository -- share
;;;; this one daemon without moving each other's state.
;;;;
;;;; Environment:
;;;;   CL_MCP_DAEMON_HOST       bind host (default "127.0.0.1")
;;;;   CL_MCP_DAEMON_PORT       bind port (default 0 = ephemeral; the
;;;;                            bridge registry makes it discoverable)
;;;;   CL_MCP_DAEMON_PORT_FILE  when set, the bound port and pid are
;;;;                            written there as "<port> <pid>" once the
;;;;                            listener is up, so the launcher can find
;;;;                            the daemon without a fixed port.

(require :asdf)

(let* ((host (or (uiop:getenv "CL_MCP_DAEMON_HOST") "127.0.0.1"))
       (port (let ((s (uiop:getenv "CL_MCP_DAEMON_PORT")))
               (if (and s (plusp (length s))) (parse-integer s) 0)))
       (port-file (uiop:getenv "CL_MCP_DAEMON_PORT_FILE")))
  ;; The pool is what makes concurrent sessions safe: one eval must not
  ;; block or crash another session's requests.
  (setf cl-mcp:*use-worker-pool* t)
  (cl-mcp:ensure-tcp-server-thread :host host :port port :accept-once nil)
  (unless cl-mcp:*tcp-server-port*
    (format *error-output* "~&cl-mcp daemon: TCP listener failed to start~%")
    (sb-ext:exit :code 1))
  (when (and port-file (plusp (length port-file)))
    (with-open-file (out port-file :direction :output
                                   :if-exists :supersede
                                   :if-does-not-exist :create)
      (format out "~D ~D~%" cl-mcp:*tcp-server-port*
              #+sbcl (sb-posix:getpid) #-sbcl 0)))
  (format *error-output* "~&cl-mcp daemon listening on ~A:~D (pid ~D)~%"
          host cl-mcp:*tcp-server-port*
          #+sbcl (sb-posix:getpid) #-sbcl 0)
  (force-output *error-output*)
  (loop (sleep 3600)))
