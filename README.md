# matrix-xmpp-bridge

A full-featured bridge for forwarding messages beetween Matrix and XMPP using Python and aioxmpp + matrix-nio.
Was originally written for personal use, so may not be very flexible in configuration.

## Features 

- [x] Forwarding simple text messages
- [x] Forwarding attachments (images/video/files etc.)
- [x] Deleting messages (only for Matrix -> XMPP, XMPP itself doesn't support deleting messages)
- [x] Editing messages
- [x] Replies to messages
- [x] Native support for specifying the sender of messages in XMPP
- [x] Marks senders in Matrix as Telegram users if it detects a t2bot bridge

## Deploy

For the bridge to work, you need to use Python 3.8+, and install all the dependencies specified in the ``requirements.txt`` file:

```
git clone https://github.com/ventureoo/matrix-xmpp-bridge.git
cd matrix-xmpp-bridge
pip install -r requirements.txt
```

The bot is configured in the ``config.yml`` file. You can use ``config.yml.sample`` as an example. For the bot to work, you must create accounts in Matrix and XMPP respectively,
then specify their login and password in configuration file. You must also join from these accounts to the required rooms, and also specify their address in the config.

IMPORTANT: For Matrix, you must specify the internal ID of the room, not its public address.

```
matrix:
  server: "https://matrix.org" #
  user: "@your_matrix_user:matrix.org"
  password: "YoUr_sEcReT_pAsS"
  room_id: "!your_matrix_room:matrix.org"

xmpp:
  jid: "your_xmpp_user@example.org"
  password: "YoUr_SEcReT_PasS"
  muc: "your_xmpp_chat_room@coversation.example.org"
```

After that, you can start the bridge.

### Docker

If you need the bridge to run in Docker, once configured you must build the container:

```
docker build . -t matrix-xmpp-bridge
```

And run it:

```
docker run matrix-xmpp-bridge
```

## Feedback

If you have any issues with how the bridge works/is configured, feel free to contact me:

- Matrix: @venture0:matrix.org
- XMPP: ventureo@5222.de
