# Ubuntu Server
Simple instructions for setting up a server via Ubuntu for our k0s cluster.

## Installation
* No LVM

### Network Interface Settings
```yaml
name: bond0
iface: en0, en1
mode: 802.3ad
xmit: layer3+4
lacp: fast
```

### Setup
The apply script will do the rest of the work. This command pulls the `apply.sh` file and executes it.

```shell
curl -sL http://bit.ly/49goNIv -o - | bash
```
