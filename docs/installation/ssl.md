## Introduction
You will want to run guacozy over SSL because of several reasons:  
- Protect login credentials
- Guacamole protocol is text base and unencrypted by itself
- Clipboard sync doesn't work over HTTP on hosts other than localhost

If both files are missing, the entrypoint writes a self-signed pair once. A pair that is already in `/ssl` is left as it is. The compose file keeps that directory on the `guacozy-ssl` volume.
  
You can provide your certificates by mounting to /ssl/ and providing  
```
/ssl/cert.crt  
/ssl/cert.key
```

### Generate self-signed certificate
Create a directory `certs` in the same directory where you docker-compose or where you will run docker run command:
```
mkdir certs
cd certs
```
Generate SSL certificates using openssl
```
openssl req -x509 -nodes -days 3650 -subj "/CN=guacozy.example.com" -addext "subjectAltName=DNS:guacozy.example.com" -newkey rsa:4096 -keyout cert.key -out cert.crt;
```

### Prepare commercial certificate
If you use your own certificate, concatenate you certificate and intermediate certificates in PEM format to single file
```
cat my-cert.pem intermediate-certs.pem > cert.crt
```
and put private key in PEM format to `cert.key`

### Mount certificate folder to container 
Now you can mount your `cert` directory using bind mount:
###### docker run
```shell script
docker run -it --rm -p 10443:443 -v ./certs:/ssl guacozy/guacozy-server
```
###### docker-compose
```yaml
services:
  server:
    image: guacozy/guacozy-server
    volumes:           
       - ./certs:/ssl  
...
```
### Option: keep the generated certificate
`docker-compose.yml` already mounts a named volume at `/ssl`:

```yaml
services:
  server:
    volumes:
      - guacozy-ssl:/ssl
volumes:
  guacozy-ssl:
```

The certificate is written only when that volume has neither `cert.crt` nor `cert.key`.
