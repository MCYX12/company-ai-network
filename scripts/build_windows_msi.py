"""Build a per-user offline MSI from a verified client ZIP (WiX 3/wixl)."""
import argparse
import importlib.util
import json
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
NS = 'http://schemas.microsoft.com/wix/2006/wi'
UPGRADE = '{18A78023-E5D6-48CD-AE7B-2316358C44C1}'
def element(parent, tag, **attrs):
    return ET.SubElement(parent, '{'+NS+'}'+tag, {k:str(v) for k,v in attrs.items()})

def build(package, signature, trusted_key, output, compiler, version='0.9.42'):
    spec=importlib.util.spec_from_file_location('package_trust', ROOT/'packaging/client/package_trust.py')
    trust=importlib.util.module_from_spec(spec);spec.loader.exec_module(trust)
    trust.verify(package, signature, trusted_key)
    # The MSI manages software only. Enrollment, runtime and owner data are outside
    # this directory and therefore survive MSI repair/uninstall/major upgrades.
    if not all(part.isdigit() for part in version.split('.')) or len(version.split('.'))!=3:
        raise ValueError('Three numeric MSI version components required')
    output=Path(output).absolute()
    with tempfile.TemporaryDirectory() as tmp:
        stage=Path(tmp)
        with zipfile.ZipFile(package) as archive:
            for name in ('install_windows.ps1','package_trust.py'):
                (stage/name).write_bytes(archive.read('company-agent-client/'+name))
        shutil.copy2(package,stage/'client.zip');shutil.copy2(signature,stage/'client.signature.json')
        (stage/'ACTIVATE.txt').write_text('Activate with install_windows.ps1, an individual enrollment, Python 3.11+, and an independently provisioned -TrustedKey.\nMSI contains software only. It does not enroll an identity, change existing clients, or start a worker.\nUninstall removes only these software files; client configuration, credentials and runtime stay outside this MSI directory.\n')
        ET.register_namespace('',NS);doc=ET.Element('{'+NS+'}Wix')
        product=element(doc,'Product',Id='*',Name='Company Agent Offline Client',Language=1033,
                        Version=version,Manufacturer='Company Agent Development',UpgradeCode=UPGRADE)
        element(product,'Package',InstallerVersion=500,Compressed='yes',InstallScope='perUser')
        element(product,'MajorUpgrade',DowngradeErrorMessage='A newer offline client is already installed.')
        element(product,'Media',Id=1,Cabinet='client.cab',EmbedCab='yes')
        directory=element(product,'Directory',Id='TARGETDIR',Name='SourceDir')
        local=element(directory,'Directory',Id='LocalAppDataFolder')
        target=element(local,'Directory',Id='INSTALLDIR',Name='CompanyAgentOfflineClient')
        feature=element(product,'Feature',Id='ClientSoftware',Title='Offline software (enrollment separate)',Level=1)
        for index,path in enumerate(sorted(stage.iterdir())):
            cid='Asset'+str(index)
            guid='{'+str(uuid.uuid5(uuid.UUID(UPGRADE.strip('{}')),path.name)).upper()+'}'
            component=element(target,'Component',Id=cid,Guid=guid,Win64='yes')
            element(component,'File',Id='File'+str(index),Name=path.name,Source=str(path),DiskId=1)
            element(component,'RegistryValue',Root='HKCU',Key='Software\\CompanyAgent\\OfflineClient',Name=path.name,Type='string',Value=version,KeyPath='yes')
            element(feature,'ComponentRef',Id=cid)
        wxs=stage/'client.wxs';ET.ElementTree(doc).write(wxs,encoding='utf-8',xml_declaration=True)
        output.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run([str(compiler),'-a','x64','-o',str(output),str(wxs)],check=True)
        output.with_suffix('.wxs').write_bytes(wxs.read_bytes())
    return {'msi':str(output),'version':version,'scope':'per_user_software_only','enrollment_embedded':False}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('package','signature','trusted-key','output','compiler'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--version',default='0.9.42');a=p.parse_args()
    print(json.dumps(build(a.package,a.signature,a.trusted_key,a.output,a.compiler,a.version)))
