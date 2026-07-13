# getoffline-sdk

Python 3.11+ SDK for the GetOffline API.

## Install from a wheel

Build and install the package from this directory:

```bash
python -m pip wheel . -w dist
python -m pip install dist/getoffline_sdk-0.1.0-py3-none-any.whl
```

## Usage

```python
from getoffline_sdk import GetOfflineClient, HttpTransport

client = GetOfflineClient(HttpTransport("https://example.com/api"))
library = client.library()
```

For in-process Django tests or monolith deployments, install the optional Django extra and use `DjangoTransport`:

```bash
python -m pip install "getoffline-sdk[django]"
```
