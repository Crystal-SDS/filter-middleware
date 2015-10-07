# SDS-Storlet-Middleware

## Installation

To install the module you can run the next line in the parent folder:
```python
python setup.py install
```

After that, it is necessary to configure OpenStack Swift to add the middleware in the proxy. Open the configure file of proxy ( `proxy-server.conf`):

- We need to add a new filter that must be called storlets_swift, you can copying the next lines in the bellow part of the file:
```
[filter:storlets_swift]
use = egg:storlets_swift#storlets_swift
```
- Also it is necessary to add this filter in the pipeline variable. This filter must be
added before `storlet_handler` filter.

- The last step is restart the proxy-server service. Now the middleware has been installed.
