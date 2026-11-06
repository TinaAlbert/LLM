/* AppContainer原生载荷：有限观察或委托候选回传，无网络模型、凭证或业务动作。 */
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0602
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int line(char *buffer, size_t capacity) {
    if (!fgets(buffer, (int)capacity, stdin)) return 0;
    size_t n = strlen(buffer);
    if (n == 0 || buffer[n-1] != '\n') return 0;
    buffer[--n] = 0;
    if (n && buffer[n-1] == '\r') buffer[--n] = 0;
    return 1;
}
#ifndef EXPLORE_ONLY
static int object_id(const char *s) {
    if (strlen(s) != 36) return 0;
    for (int i=0;i<36;i++) {
        if (i==8 || i==13 || i==18 || i==23) {if(s[i]!='-')return 0;}
        else if (!((s[i]>='0' && s[i]<='9') || (s[i]>='a' && s[i]<='f'))) return 0;
    }
    return 1;
}
#endif
enum { GUIDE_CANDIDATE_LIMIT = 16384 };
static int guide_reference(const char *s) {
    if (strlen(s) != 32) return 0;
    for (int i=0;i<32;i++) {
        if (!((s[i]>='0' && s[i]<='9') || (s[i]>='a' && s[i]<='f'))) return 0;
    }
    return 1;
}
static int guide(const char *reference, const char *fault) {
    char input[GUIDE_CANDIDATE_LIMIT+64];
    if (!guide_reference(reference)) return 12;
    printf("guide-request\t%s\n",reference);
    /* 模型网络等待由可信宿主计时；此处只等有限候选，不自行启动网络或重试。 */
    if (!line(input,sizeof(input)) || strlen(input)<51 ||
            strncmp(input,"guide-candidate\t",16)!=0 ||
            strncmp(input+16,reference,32)!=0 || input[48]!='\t') return 12;
    const char *candidate=input+49;size_t size=strlen(candidate);
    if (size<2 || size>GUIDE_CANDIDATE_LIMIT || candidate[0]!='{' || candidate[size-1]!='}') return 12;
    for(size_t i=0;i<size;i++) if((unsigned char)candidate[i]<32) return 12;
    if (!MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,candidate,(int)size,NULL,0)) return 12;
    /* 宿主先做严格Schema/引用校验，再以原文匹配核对；外壳没有业务写能力。 */
    printf("guide-result\t%s\t%s\n",reference,candidate);
    if(fault && strcmp(fault,"extra")==0)puts("untrusted_extra");
    if(fault && strcmp(fault,"nonzero")==0)return 23;
    if(fault && strcmp(fault,"hang")==0)Sleep(INFINITE);
    return 0;
}

#ifdef EXPLORE_ONLY
/* P22独立产物：动作只有语义白名单；每个进程只确认一条，再以正常EOF退出。 */
enum { EXPLORE_OBSERVATION_LIMIT = 12288, EXPLORE_OPERATION_LIMIT = 28 };
static int explore(const char *reference, const char *sequence, const char *fault) {
    char input[EXPLORE_OBSERVATION_LIMIT+128],prefix[128],request[128];
    char canonical[4];unsigned long number;char *end=NULL;
    if(!guide_reference(reference) || !sequence[0])return 12;
    number=strtoul(sequence,&end,10);
    if(!end || *end || number<1 || number>EXPLORE_OPERATION_LIMIT)return 12;
    snprintf(canonical,sizeof(canonical),"%lu",number);
    if(strcmp(sequence,canonical))return 12;
    snprintf(prefix,sizeof(prefix),"explore-observation\t%s\t%s\t",reference,sequence);
    printf("explore-observe-request\t%s\t%s\n",reference,sequence);
    if(!line(input,sizeof(input)) || strncmp(input,prefix,strlen(prefix)))return 12;
    const char *observation=input+strlen(prefix);size_t size=strlen(observation);
    if(size<2 || size>EXPLORE_OBSERVATION_LIMIT || observation[0]!='{' || observation[size-1]!='}')return 12;
    /* JSON/字段语义由可信投影器验证；此边界额外拒绝控制字符与非ASCII字节。 */
    for(size_t i=0;i<size;i++)if((unsigned char)observation[i]<32 || (unsigned char)observation[i]>126)return 12;
    printf("explore-candidate-request\t%s\t%s\n",reference,sequence);
    snprintf(prefix,sizeof(prefix),"explore-action\t%s\t%s\t",reference,sequence);
    if(!line(input,sizeof(input)) || strncmp(input,prefix,strlen(prefix)))return 12;
    char *operation=input+strlen(prefix),*target=strchr(operation,'\t');
    if(!target)return 12;
    *target++=0;
    if(strchr(target,'\t'))return 12;
    if(strcmp(operation,"observe")==0) {if(strcmp(target,"-"))return 12;}
    else {
        if(strcmp(operation,"booking.quote") && strcmp(operation,"booking.lock") &&
           strcmp(operation,"booking.cancel_lock") && strcmp(operation,"booking.pay"))return 12;
        if(strcmp(target,"trip_1") && strcmp(target,"trip_2"))return 12;
    }
    snprintf(request,sizeof(request),"explore-permit\t%s\t%s\t%s\t%s",reference,sequence,operation,target);
    puts(request);
    if(fault && strcmp(fault,"extra")==0)puts("untrusted_extra");
    if(fault && strcmp(fault,"nonzero")==0)return 23;
    if(fault && strcmp(fault,"hang")==0)Sleep(INFINITE);
    return 0;
}
#endif

static void network(char *host, const char *port, int family, int datagram) {
    SOCKET sock=socket(family,datagram?SOCK_DGRAM:SOCK_STREAM,datagram?IPPROTO_UDP:IPPROTO_TCP);
    if(sock==INVALID_SOCKET) {printf("socket\t%d\n",WSAGetLastError());return;}
    struct sockaddr_storage address;memset(&address,0,sizeof(address));int length;
    if(family==AF_INET6) {
        struct sockaddr_in6 *addr=(struct sockaddr_in6*)&address;
        addr->sin6_family=AF_INET6;addr->sin6_port=htons((unsigned short)atoi(port));length=sizeof(*addr);
        if(InetPtonA(AF_INET6,host,&addr->sin6_addr)!=1){closesocket(sock);puts("invalid_address");return;}
    } else {
        struct sockaddr_in *addr=(struct sockaddr_in*)&address;
        addr->sin_family=AF_INET;addr->sin_port=htons((unsigned short)atoi(port));length=sizeof(*addr);
        if(InetPtonA(AF_INET,host,&addr->sin_addr)!=1){closesocket(sock);puts("invalid_address");return;}
    }
    u_long nonblocking=1;ioctlsocket(sock,FIONBIO,&nonblocking);
    if(datagram) {
        /* 固定无私人数据的example.com DNS查询；发送完成不等于送达。 */
        static const unsigned char marker[]={0xd2,0x02,0x01,0,0,1,0,0,0,0,0,0,7,'e','x','a','m','p','l','e',3,'c','o','m',0,0,1,0,1};
        int sent=sendto(sock,(const char*)marker,sizeof(marker),0,(struct sockaddr*)&address,length);
        if(sent==SOCKET_ERROR){printf("socket\t%d\n",WSAGetLastError());closesocket(sock);return;}
        fd_set input,failed;FD_ZERO(&input);FD_SET(sock,&input);failed=input;
        struct timeval timeout={2,0};int ready=select(0,&input,NULL,&failed,&timeout);
        if(ready==0)puts("udp-no-response");
        else if(ready==SOCKET_ERROR)printf("socket\t%d\n",WSAGetLastError());
        else {
            char response[4096];int received=recvfrom(sock,response,sizeof(response),0,NULL,NULL);
            if(received==SOCKET_ERROR)printf("socket\t%d\n",WSAGetLastError());else puts("ALLOWED");
        }
        closesocket(sock);return;
    }
    int rc=connect(sock,(struct sockaddr*)&address,length);
    int error=rc==0?0:WSAGetLastError();
    if(error==WSAEWOULDBLOCK) {
        fd_set output,failed;FD_ZERO(&output);FD_SET(sock,&output);failed=output;
        struct timeval timeout={2,0};rc=select(0,NULL,&output,&failed,&timeout);
        if(rc==0){puts("timeout");closesocket(sock);return;}
        if(rc==SOCKET_ERROR){printf("socket\t%d\n",WSAGetLastError());closesocket(sock);return;}
        int size=sizeof(error);
        if(getsockopt(sock,SOL_SOCKET,SO_ERROR,(char*)&error,&size)==SOCKET_ERROR)error=WSAGetLastError();
    }
    if(error==0)puts("ALLOWED");else printf("socket\t%d\n",error);
    closesocket(sock);
}
static void spawn(void) {
    wchar_t path[32768],command[32770];STARTUPINFOW startup;PROCESS_INFORMATION process;
    if(!GetModuleFileNameW(NULL,path,32768)){printf("spawn\t%lu\n",GetLastError());return;}
    HANDLE file=CreateFileW(path,GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,0,NULL);
    if(file==INVALID_HANDLE_VALUE){puts("self_unavailable");return;}CloseHandle(file);
    memset(&startup,0,sizeof(startup));memset(&process,0,sizeof(process));startup.cb=sizeof(startup);
    if(swprintf(command,32770,L"\"%ls\"",path)<0){puts("self_unavailable");return;}
    if(CreateProcessW(path,command,NULL,NULL,FALSE,DETACHED_PROCESS,NULL,NULL,&startup,&process)) {
        TerminateProcess(process.hProcess,14);WaitForSingleObject(process.hProcess,3000);
        CloseHandle(process.hThread);CloseHandle(process.hProcess);puts("ALLOWED");
    } else printf("spawn\t%lu\n",GetLastError());
}
static int probe(void) {
    char input[32770];
    while (line(input,sizeof(input)) && strcmp(input,"end")!=0) {
        if (strncmp(input,"file\t",5)==0) {
            wchar_t path[32768];
            if (!MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,input+5,-1,path,32768)) return 12;
            HANDLE file=CreateFileW(path,GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,
                                    NULL,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
            if(file!=INVALID_HANDLE_VALUE) {CloseHandle(file);puts("ALLOWED");}
            else printf("denied\t%lu\n",GetLastError());
        } else if (strncmp(input,"tcp\t",4)==0 || strncmp(input,"tcp6\t",5)==0 ||
                   strncmp(input,"udp\t",4)==0 || strncmp(input,"udp6\t",5)==0) {
            int family=input[3]=='6'?AF_INET6:AF_INET,datagram=input[0]=='u';
            char *host=strchr(input,'\t')+1,*port=strchr(host,'\t');if(!port)return 12;*port++=0;
            network(host,port,family,datagram);
        } else if (strcmp(input,"spawn")==0) {
            spawn();
        } else if (strncmp(input,"environment\t",12)==0) {
            const char *name=input+12;size_t size=strlen(name);if(size==0 || size>64)return 12;
            for(size_t i=0;i<size;i++) {
                if(!((name[i]>='A' && name[i]<='Z') || (name[i]>='0' && name[i]<='9') || name[i]=='_'))return 12;
            }
            puts(getenv(name)==NULL?"environment\tabsent":"environment\tpresent");
        } else if (strncmp(input,"fault\t",6)==0) {
            const char *mode=input+6;
            if(strcmp(mode,"exit")==0)return 23;
            if(strcmp(mode,"silent")==0)Sleep(INFINITE);
            if(strcmp(mode,"partial")==0){fputs("partial",stdout);return 0;}
            if(strcmp(mode,"utf8")==0){fputc(0xff,stdout);fputc('\n',stdout);return 0;}
            if(strcmp(mode,"long")==0){char output[70000];memset(output,'x',sizeof(output));fwrite(output,1,sizeof(output),stdout);puts("");return 0;}
            if(strcmp(mode,"flood")==0){for(int i=0;i<32;i++)puts("extra");Sleep(INFINITE);}
            return 12;
        } else if (strncmp(input,"reply\t",6)==0) {
            char observation[32770];const char *mode=input+6;
            if(strcmp(mode,"good") && strcmp(mode,"extra") && strcmp(mode,"nonzero") && strcmp(mode,"hang"))return 12;
            if(!line(observation,sizeof(observation)))return 12;
            printf("result\t%s\n",observation);
            if(strcmp(mode,"extra")==0)puts("untrusted_extra");
            if(strcmp(mode,"nonzero")==0)return 23;
            if(strcmp(mode,"hang")==0)Sleep(INFINITE);
            return 0;
#ifdef EXPLORE_ONLY
        } else if (strncmp(input,"explore-reply\t",14)==0) {
            char *mode=input+14,*reference=strchr(mode,'\t'),*sequence;
            if(!reference)return 12;
            *reference++=0;
            sequence=strchr(reference,'\t');if(!sequence)return 12;
    *sequence++=0;
            if(strcmp(mode,"good") && strcmp(mode,"extra") && strcmp(mode,"nonzero") && strcmp(mode,"hang"))return 12;
            return explore(reference,sequence,mode);
#endif
        } else if (strncmp(input,"guide-reply\t",12)==0) {
            char *mode=input+12,*reference=strchr(mode,'\t');if(!reference)return 12;
            *reference++=0;
            if(strcmp(mode,"good") && strcmp(mode,"extra") && strcmp(mode,"nonzero") && strcmp(mode,"hang"))return 12;
            return guide(reference,mode);
        } else if (strncmp(input,"stall\t",6)==0) {
            printf("observe\t%s\n",input+6);Sleep(INFINITE);
        } else if (strncmp(input,"emit\t",5)==0) {
            /* 工程反例：实际受限进程发出非法工具请求，交由同一可信分派器拒绝。 */
            puts(input+5);
        } else return 12;
    }
    return 0;
}
int main(int argc,char **argv) {
    HANDLE token;DWORD is_container=0,returned=0;
    if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token))return 10;
    BOOL ok=GetTokenInformation(token,TokenIsAppContainer,&is_container,sizeof(is_container),&returned);
    CloseHandle(token);if(!ok || !is_container)return 11;
    setvbuf(stdout,NULL,_IONBF,0);puts("ready\tappcontainer");
    if(argc==2 && strcmp(argv[1],"--probe")==0) {
        WSADATA data;if(WSAStartup(MAKEWORD(2,2),&data))return 13;
        int result=probe();WSACleanup();return result;
    }
#ifdef EXPLORE_ONLY
    char selected[128];
    if(!line(selected,sizeof(selected)) || strncmp(selected,"start-explore\t",14))return 12;
    char *reference=selected+14,*sequence=strchr(reference,'\t');
    if(!sequence)return 12;
    *sequence++=0;
    return explore(reference,sequence,NULL);
#else
    char selected[128],observation[32770];
    if(!line(selected,sizeof(selected)))return 12;
    if(strncmp(selected,"start-guide\t",12)==0)return guide(selected+12,NULL);
    char *id=strchr(selected,'\t');if(!id)return 12;*id++=0;
    if((strcmp(selected,"trip") && strcmp(selected,"approval")) || !object_id(id))return 12;
    printf("observe\t%s\t%s\n",selected,id);
    if(!line(observation,sizeof(observation)))return 12;
    printf("result\t%s\n",observation);
    return 0;
#endif
}
