"""An interrupted update must restore exact bytes; later edits must stop rollback."""
import hashlib
from pathlib import Path
import tempfile

import install


def row(path,data):
    return {'path':str(path),'data':data,'before_sha256':install.sha(path),
            'after_sha256':hashlib.sha256(data).hexdigest(),'mode':0o600}


def check():
    with tempfile.TemporaryDirectory(prefix='ninax-install-') as directory:
        root=Path(directory)
        config=root/'config.yaml';config.write_bytes(b'preserved settings\n')
        plugin=root/'plugins/line-platform/plugin.yaml'
        receipt=root/'release.json'
        plan=[row(config,b'updated settings\n'),row(plugin,b'kind: platform\n')]
        install.apply(plan,receipt)
        plugin.write_bytes(b'new local change\n')
        try:
            install.rollback(receipt)
            raise AssertionError('rollback must preflight all files before restoring any')
        except ValueError as exc:
            assert str(exc)=='rollback_refuses_drift'
        assert config.read_bytes()==b'updated settings\n'
        plugin.write_bytes(plan[1]['data'])
        install.rollback(receipt)
        assert config.read_bytes()==b'preserved settings\n' and not plugin.exists()
        assert list(plugin.parent.glob('plugin.yaml.rollback-*'))
        # A crash after only the first replacement is recoverable from the prepared receipt.
        partial=root/'partial.json'
        backup=root/'original';backup.write_bytes(config.read_bytes())
        plan=[row(config,b'interrupted update'),row(root/'new.py',b'print(1)')]
        install.save(partial,{'status':'prepared','files':[
            {k:v for k,v in item.items() if k!='data'}|{'backup':str(backup) if i==0 else None}
            for i,item in enumerate(plan)]})
        install.replace(config,plan[0]['data'])
        install.rollback(partial)
        assert config.read_bytes()==b'preserved settings\n' and not (root/'new.py').exists()
    print('INSTALL_ROLLBACK_PASS: exact restoration, retained new files, drift refusal, interrupted apply')


if __name__=='__main__':
    check()
