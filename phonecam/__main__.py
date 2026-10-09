from .app import main

# Guarded: background-render workers re-import this module when they spawn.
if __name__ == '__main__':
    raise SystemExit(main())
