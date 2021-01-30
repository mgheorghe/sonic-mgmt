FROM archlinux/base

RUN pacman --noconfirm  -Sy
RUN pacman --noconfirm  -Syu

RUN pacman --noconfirm --needed -S base-devel
RUN pacman --noconfirm --needed -S git
RUN pacman --noconfirm --needed -S wget

RUN pacman --noconfirm --needed -S python
RUN pacman --noconfirm --needed -S python-setuptools
RUN pacman --noconfirm --needed -S python-pip
RUN pacman --noconfirm --needed -S tk
RUN pacman --noconfirm --needed -S tcl

RUN useradd -m yay
RUN usermod -a -G root yay
RUN echo -e '\nALL     ALL=(ALL) 	NOPASSWD: ALL\n' >> /etc/sudoers

RUN git clone https://aur.archlinux.org/yay.git
RUN chmod 777 -R /yay
WORKDIR /yay
RUN sudo -H -u yay makepkg --noconfirm --needed -si

RUN sudo -H -u yay yay --noconfirm -S tclx
RUN sudo -H -u yay yay --noconfirm -S tcllib
RUN sudo -H -u yay yay --noconfirm -S tcltls

RUN pip install --upgrade setuptools
RUN pip install --upgrade wheel

COPY . /keysight
WORKDIR /keysight

RUN pip install -r ./requirements.txt

RUN patch -b /usr/lib/tcl8/8.6/http-2.9.5.tm ./http-2.9.0.tm.patch

RUN wget http://downloads.ixiacom.com/support/downloads_and_updates/public/ixnetwork/9.10/IxNetworkAPI9.10.2007.7Linux64.bin.tgz
RUN tar -xvf ./IxNetworkAPI9.10.2007.7Linux64.bin.tgz
RUN bash ./IxNetworkAPI9.10.2007.7Linux64.bin -i silent

RUN wget https://sourceforge.net/projects/mpexpr/files/mpexpr/1.2/mpexpr-1.2.tar.gz
RUN tar -zxvf mpexpr-1.2.tar.gz
WORKDIR /keysight/mpexpr-1.2/unix
RUN bash ./configure --prefix=/usr/share  --exec-prefix=/usr
RUN make
RUN make test
RUN make install

WORKDIR /keysight

RUN rm -rf ./*
RUN pacman --noconfirm -Scc

LABEL author="Mircea Dan Gheorghe"
LABEL version="1.0"
LABEL description="SpyTest with Keysight traffic generator"