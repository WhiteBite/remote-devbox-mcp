FROM node:22-alpine

RUN npm install -g localtunnel

CMD ["sh", "-c", "lt --port 8799 --local-host host.docker.internal ${RELAY_SUBDOMAIN:+--subdomain $RELAY_SUBDOMAIN}"]