# moonraker_TCP_Gcode_bridge

<img width="850" height="447" alt="1000017505" src="https://github.com/user-attachments/assets/ccfbfe0f-eee9-4369-83cc-0854efc27085" />


TCP 포트를 통해 Moonraker에 G-code를 전송하는 브리지입니다.  
TCP 연결을 지원하는 LightBurn과 Pronterface에서 테스트되었습니다.

This project provides a TCP bridge for sending G-code directly to Moonraker.  
It has been tested with TCP-capable clients such as LightBurn and Pronterface.

# How To use

기본적으로 TCP 4125 포트를 사용하여 통신합니다.  
웹 콘솔은 8080 포트로 접속할 수 있습니다.

By default, the bridge listens on TCP port 4125.  
The web console is available on port 8080.


## References

This project was inspired by the Klipperotchy project and other Moonraker/JSON-RPC documentation.

- Klipperotchy — https://github.com/shishu94/Klipperotchy
- Moonraker API Documentation — https://moonraker.readthedocs.io/
- Python websockets library — https://github.com/python-websockets/websockets
