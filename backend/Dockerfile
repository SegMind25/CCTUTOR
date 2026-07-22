FROM gcc:13

RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ make libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json .
RUN npm install --production
COPY server.js .

EXPOSE 3001
CMD ["node", "server.js"]
