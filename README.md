# MNVUSAGE - Monitor NV Usage
For webview to monitor your nvidia gpu status

<p align="center">
	<img src="pic/logo.jpeg" alt="logo" width="480">

## Just want to try
'''
python main.py
'''

-----

## Use Docker to Host
### Build a container
'''
docker build -t mnvusage .
'''

### Run
'''
docker run -d -p 5555:5000 --gpus all --name mnvusage-container mnvusage
'''

### View

Go to http://localhost:5555

<p align="center">
	<img src="pic/mnvusage.png" alt="demo" width="480">
