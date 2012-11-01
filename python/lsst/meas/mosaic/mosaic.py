import re
import os, math
import datetime
import numpy

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import lsst.afw.cameraGeom              as cameraGeom
import lsst.afw.cameraGeom.utils        as cameraGeomUtils
import lsst.afw.image                   as afwImage
import lsst.afw.coord                   as afwCoord
import lsst.afw.math                    as afwMath
import lsst.afw.table                   as afwTable
import lsst.afw.geom                    as afwGeom
import lsst.meas.algorithms.utils       as malgUtils
import lsst.meas.astrom.astrom          as measAstrom
import lsst.meas.mosaic.mosaicLib       as measMosaic
import lsst.meas.mosaic.config          as measMosaicConfig

def readCcd(camera, ccdIds):

    print "Reading CCD info ..."

    ccds = measMosaic.CcdSet()
    for i in ccdIds:
        ccd = cameraGeomUtils.findCcd(camera, cameraGeom.Id(int(i)))
        ccds[i] = ccd

    # Calculate mean position of all CCD chips
    sx = sy = 0.
    for ccd in ccds.values():
        center = ccd.getCenter().getPixels(ccd.getPixelSize())
        sx += center[0]
        sy += center[1]
    dx = sx / ccds.size()
    dy = sy / ccds.size()

    # Shift the origin of CCD chips
    for ccd in ccds.values():
        pixelSize = ccd.getPixelSize()
        ccd.setCenter(ccd.getCenter()
                      - cameraGeom.FpPoint(dx*pixelSize, dy*pixelSize)
                      - cameraGeom.FpPoint(ccd.getAllPixels(True).getWidth()*0.5,
                                           ccd.getAllPixels(True).getHeight()*0.5))
        
    print datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")

    return ccds

def getWcsForCcd(butler, frame, ccd):

    data = {'visit': frame, 'ccd':ccd}

    try:
        md = butler.get('calexp_md', data)
        return afwImage.makeWcs(md)
    except Exception, e:
        print "Failed to read: %s for %s" % (e, data)
        return None

def readWcs(butler, frameIds, ccdSet):
        
    print "Reading WCS ..."

    wcsDic = measMosaic.WcsDic()
    for frameId in frameIds:
        found = 0
        for ccdId in ccdSet.keys():
            ccd = ccdSet[ccdId]
            dataId = {'visit': frameId, 'ccd': ccdId}
            if (butler.datasetExists('calexp',  dataId) and
                butler.datasetExists('src',     dataId) and
                butler.datasetExists('icSrc',   dataId) and
                butler.datasetExists('icMatch', dataId)):
                found = 1
                break
        if found:
            wcs = getWcsForCcd(butler, frameId, ccdId)
        else:
            wcs = None
        if wcs != None:
            offset = ccd.getCenter().getPixels(ccd.getPixelSize())
            wcs.shiftReferencePixel(offset[0], offset[1])
            wcsDic[frameId] = wcs

    print datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")

    return wcsDic

def removeNonExistCcd(butler, ccdSet, wcsDic):
    num = dict()
    for ichip in ccdSet.keys():
        num[ichip] = 0

    for iexp in wcsDic.keys():
        for ichip in ccdSet.keys():
            dataId = {'visit': iexp, 'ccd': ichip}
            if (butler.datasetExists('calexp',  dataId) and
                butler.datasetExists('src',     dataId) and
                butler.datasetExists('icSrc',   dataId) and
                butler.datasetExists('icMatch', dataId)):
                num[ichip] += 1

    for ichip in ccdSet.keys():
        if num[ichip] == 0:
            ccdSet.erase(ichip)
            
def selectStars(sources, includeSaturated=False):
    if len(sources) == 0:
        return []
    if isinstance(sources, afwTable.SourceCatalog):
        extended = sources.columns["classification.extendedness"]
        saturated = sources.columns["flags.pixel.saturated.center"]
        try:
            nchild = sources.columns["deblend.nchild"]
        except:
            nchild = numpy.zeros(len(sources))
        indices = numpy.where(numpy.logical_and(numpy.logical_and(extended < 0.5, saturated == False), nchild == 0))[0]
        return [sources[int(i)] for i in indices]

    psfKey = None                       # Table key for classification.psfstar
    if isinstance(sources, afwTable.ReferenceMatchVector) or isinstance(sources[0], afwTable.ReferenceMatch):
        sourceList = [s.second for s in sources]
        psfKey = sourceList[0].schema.find("classification.psfstar").getKey()
    else:
        sourceList = sources

    schema = sourceList[0].schema
    extKey = schema.find("classification.extendedness").getKey()
    satKey = schema.find("flags.pixel.saturated.center").getKey()

    stars = []
    for includeSource, checkSource in zip(sources, sourceList):
        star = (psfKey is not None and checkSource.get(psfKey)) or checkSource.get(extKey) < 0.5
        saturated = checkSource.get(satKey)
        if star and (includeSaturated or not saturated):
            stars.append(includeSource)
    return stars

def getAllForCcd(butler, astrom, frame, ccd, ct=None):

    data = {'visit': frame, 'ccd': ccd}

    try:
        if not butler.datasetExists('src', data):
            raise RuntimeError("no data for src %s" % (data))
        if not butler.datasetExists('calexp_md', data):
            raise RuntimeError("no data for calexp_md %s" % (data))
        md = butler.get('calexp_md', data)
        wcs = afwImage.makeWcs(md)

        sources = butler.get('src', data)
        if False:
            matches = measAstrom.readMatches(butler, data)
        else:
            icSrces = butler.get('icSrc', data)
            packedMatches = butler.get('icMatch', data)
            matches = astrom.joinMatchListWithCatalog(packedMatches, icSrces, True)
            if ct != None:
                if matches[0].first != None:
                    refSchema = matches[0].first.schema
                else:
                    refSchema = matches[1].first.schema
                key_p = refSchema.find(ct.primary).key
                key_s = refSchema.find(ct.secondary).key
                key_f = refSchema.find("flux").key
                for m in matches:
                    if m.first != None:
                        refFlux1 = m.first.get(key_p)
                        refFlux2 = m.first.get(key_s)
                        refMag1 = -2.5*math.log10(refFlux1)
                        refMag2 = -2.5*math.log10(refFlux2)
                        refMag = ct.transformMags(ct.primary, refMag1, refMag2)
                        refFlux = math.pow(10.0, -0.4*refMag)
                        if refFlux == refFlux:
                            m.first.set(key_f, refFlux)
                        else:
                            m.first = None

        sources = selectStars(sources)
        selMatches = selectStars(matches)
        if len(selMatches) < 10:
            matches = selectStars(matches, True)
        else:
            matches = selMatches
    except Exception, e:
        print "Failed to read: %s" % (e)
        return None, None, None
    
    return sources, matches, wcs

def readCatalog(butler, frameIds, ccdIds, ct=None):
    print "Reading catalogs ..."
    sourceSet = measMosaic.SourceGroup()
    matchList = measMosaic.SourceMatchGroup()
    astrom = measAstrom.Astrometry(measAstrom.MeasAstromConfig())
    for frameId in frameIds:
        ss = []
        ml = []
        for ccdId in ccdIds:
            sources, matches, wcs = getAllForCcd(butler, astrom, frameId, ccdId, ct)
            if sources != None:
                for s in sources:
                    if numpy.isfinite(s.getRa().asDegrees()): # get rid of NaN
                        src = measMosaic.Source(s)
                        src.setExp(frameId)
                        src.setChip(ccdId)
                        ss.append(src)
                for m in matches:
                    if m.first != None and m.second != None:
                        match = measMosaic.SourceMatch(measMosaic.Source(m.first, wcs), measMosaic.Source(m.second))
                        match.second.setExp(frameId)
                        match.second.setChip(ccdId)
                        ml.append(match)
        sourceSet.push_back(ss)
        matchList.push_back(ml)
    print datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")

    return sourceSet, matchList

def countObsInSourceGroup(sg):
    num = 0
    for s in sg:
        num += (len(s) - 1)

    return num

def mergeCatalog(sourceSet, matchList, ccdSet, d_lim, nbrightest):

    print "Creating kd-tree for matched catalog ..."
    print 'len(matchList) = ', len(matchList), [len(matches) for matches in matchList]
    rootMat = measMosaic.kdtreeMat(matchList)
    #rootMat.printMat()
    allMat = rootMat.mergeMat()
    print "# of allMat : ", countObsInSourceGroup(allMat)
    print 'len(allMat) = ', len(allMat)
    print datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")
    
    print "Creating kd-tree for source catalog ..."
    print 'len(sourceSet) = ', len(sourceSet), [len(sources) for sources in sourceSet]
    rootSource = measMosaic.kdtreeSource(sourceSet, rootMat, ccdSet, d_lim, nbrightest)
    #rootSource.printSource()
    allSource = rootSource.mergeSource()
    print "# of allSource : ", countObsInSourceGroup(allSource)
    print 'len(allSource) = ', len(allSource)
    print datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")

    return allMat, allSource

def writeNewWcs(butler, coeffSet, ccdSet, fexp, fchip):
    print "Write New WCS ..."
    exp = afwImage.ExposureI(0,0)
    for iexp in coeffSet.keys():
        for ichip in ccdSet.keys():
            c = measMosaic.convertCoeff(coeffSet[iexp], ccdSet[ichip]);
            wcs = measMosaic.wcsFromCoeff(c);
            exp.setWcs(wcs)
            scale = fexp[iexp] * fchip[ichip]
            calib = afwImage.Calib()
            calib.setFluxMag0(1.0/scale)
            exp.setCalib(calib)
            try:
                butler.put(exp, 'wcs', dict(visit=iexp, ccd=ichip))
            except Exception, e:
                print "failed to write something: %s" % (e)

    print datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")

def writeDetJImg(butler, coeffSet, ccdSet):
    print "Write detJ Imgs ..."
    for iexp in coeffSet.keys():
        for ichip in ccdSet.keys():
            img = measMosaic.getJImg(coeffSet[iexp], ccdSet[ichip])
            exp = afwImage.ExposureF(afwImage.MaskedImageF(img))
            try:
                butler.put(exp, 'detj', dict(visit=iexp, ccd=ichip))
            except Exception, e:
                print "failed to write something: %s" % (e)

    print datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")

def writeDCorImg(butler, coeffSet, ccdSet, frameIds, ccdIds, ffp):
    print "Write DCor Imgs ..."
    for iexp in coeffSet.keys():
        for ichip in ccdSet.keys():
            img = measMosaic.getFCorImg(ffp, ccdSet[ichip], coeffSet[iexp])
            exp = afwImage.ExposureF(afwImage.MaskedImageF(img))
            try:
                butler.put(exp, 'dcor', dict(visit=iexp, ccd=ichip))
            except Exception, e:
                print "failed to write something: %s" % (e)

    print datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")

def writeFcr(butler, coeffSet, ccdSet, fexp, fchip, ffp):
    print "Write Fcr ..."
    for iexp in coeffSet.keys():
        for ichip in ccdSet.keys():
            newP = measMosaic.convertFluxFitParams(coeffSet[iexp], ccdSet[ichip],
                                                   measMosaic.FluxFitParams(ffp))
            metadata = measMosaic.metadataFromFluxFitParams(newP)
            exp = afwImage.ExposureI(0,0)
            exp.getMetadata().combine(metadata)
            scale = fexp[iexp] * fchip[ichip]
            calib = afwImage.Calib()
            calib.setFluxMag0(1.0/scale)
            exp.setCalib(calib)
            try:
                butler.put(exp, 'fcr', dict(visit=iexp, ccd=ichip))
            except Exception, e:
                print "failed to write something: %s" % (e)

    print datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")

def plotJCont(num, coeff, ccdSet, outputDir):
    scale = coeff.pixelScale()
    deg2pix = 1. / scale

    delta = 300.
    if (ccdSet.size() >= 100):
        x = numpy.arange(-18000., 18000., delta)
        y = numpy.arange(-18000., 18000., delta)
        levels = numpy.linspace(0.81, 1.02, 36)
    else:
        x = numpy.arange(-6000., 6000., delta)
        y = numpy.arange(-6000., 6000., delta)
        levels = numpy.linspace(0.88, 1.02, 36)
    X, Y = numpy.meshgrid(x, y)
    Z = numpy.zeros((len(X),len(Y)))

    for j in range(len(Y)):
        for i in range(len(X)):
            Z[i][j] = coeff.detJ(X[i][j], Y[i][j]) * deg2pix ** 2

    plt.clf()
    plt.contourf(X, Y, Z, levels=levels)
    plt.colorbar()

    for ccd in ccdSet.values():
        w = ccd.getAllPixels(True).getWidth()
        h = ccd.getAllPixels(True).getHeight()
        x0 = ccd.getCenter().getPixels(ccd.getPixelSize())[0] + coeff.x0
        y0 = ccd.getCenter().getPixels(ccd.getPixelSize())[1] + coeff.y0
        t0 = ccd.getOrientation().getYaw()
        x = numpy.array([x0, \
                         x0 + w * math.cos(t0), \
                         x0 + w * math.cos(t0) - h * math.sin(t0), \
                         x0 - h * math.sin(t0), \
                         x0])
        y = numpy.array([y0, \
                         y0 + w * math.sin(t0), \
                         y0 + w * math.sin(t0) + h * math.cos(t0), \
                         y0 + h * math.cos(t0), \
                         y0])
        plt.plot(x, y, 'k-')
        
    plt.savefig(os.path.join(outputDir, "jcont_%d.png" % (num)), format='png')

def plotFCorCont(num, coeff, ccdSet, ffp, outputDir):
    delta = 300.
    if (ccdSet.size() >= 100):
        x = numpy.arange(-18000., 18000., delta)
        y = numpy.arange(-18000., 18000., delta)
        levels = numpy.linspace(0.81, 1.02, 36)
    else:
        x = numpy.arange(-6000., 6000., delta)
        y = numpy.arange(-6000., 6000., delta)
        levels = numpy.linspace(0.86, 1.14, 36)
    X, Y = numpy.meshgrid(x, y)
    Z = numpy.zeros((len(X),len(Y)))

    for j in range(len(Y)):
        for i in range(len(X)):
            Z[i][j] = 10**(-0.4*ffp.eval(X[i][j], Y[i][j]))

    plt.clf()
    plt.contourf(X, Y, Z, levels=levels)
    plt.colorbar()

    for ccd in ccdSet.values():
        w = ccd.getAllPixels(True).getWidth()
        h = ccd.getAllPixels(True).getHeight()
        x0 = ccd.getCenter().getPixels(ccd.getPixelSize())[0] + coeff.x0
        y0 = ccd.getCenter().getPixels(ccd.getPixelSize())[1] + coeff.y0
        t0 = ccd.getOrientation().getYaw()
        x = numpy.array([x0, \
                         x0 + w * math.cos(t0), \
                         x0 + w * math.cos(t0) - h * math.sin(t0), \
                         x0 - h * math.sin(t0), \
                         x0])
        y = numpy.array([y0, \
                         y0 + w * math.sin(t0), \
                         y0 + w * math.sin(t0) + h * math.cos(t0), \
                         y0 + h * math.cos(t0), \
                         y0])
        plt.plot(x, y, 'k-')
        
    plt.savefig(os.path.join(outputDir, "fcont_%d.png" % (num)), format='png')

def saveResPos3(matchVec, sourceVec, num, coeff, ccdSet, outputDir):
    _xm = []
    _ym = []
    _dxm = []
    _dym = []
    for i in range(matchVec.size()):
        if (matchVec[i].good == True and matchVec[i].iexp == num):
            _xm.append(matchVec[i].u)
            _ym.append(matchVec[i].v)
            _dxm.append((matchVec[i].xi_fit - matchVec[i].xi) * 3600)
            _dym.append((matchVec[i].eta_fit - matchVec[i].eta) * 3600)
    _xs = []
    _ys = []
    _dxs = []
    _dys = []
    if (sourceVec != None):
        for i in range(sourceVec.size()):
            if (sourceVec[i].good == True and sourceVec[i].iexp == num):
                _xs.append(sourceVec[i].u)
                _ys.append(sourceVec[i].v)
                _dxs.append((sourceVec[i].xi_fit - sourceVec[i].xi) * 3600)
                _dys.append((sourceVec[i].eta_fit - sourceVec[i].eta) * 3600)

    xm = numpy.array(_xm)
    ym = numpy.array(_ym)
    dxm = numpy.array(_dxm)
    dym = numpy.array(_dym)
    xs = numpy.array(_xs)
    ys = numpy.array(_ys)
    dxs = numpy.array(_dxs)
    dys = numpy.array(_dys)

    plt.clf()
    plt.rc('text', usetex=True)

    q = plt.quiver(xm, ym, dxm, dym, units='inches', angles='xy', scale=1, color='green')
    plt.quiverkey(q, 0, 4500, 0.1, "0.1 arcsec", coordinates='data', color='black')
    plt.quiver(xs, ys, dxs, dys, units='inches', angles='xy', scale=1, color='red')

    for ccd in ccdSet.values():
        w = ccd.getAllPixels(True).getWidth()
        h = ccd.getAllPixels(True).getHeight()
        x0 = ccd.getCenter().getPixels(ccd.getPixelSize())[0] + coeff.x0
        y0 = ccd.getCenter().getPixels(ccd.getPixelSize())[1] + coeff.y0
        t0 = ccd.getOrientation().getYaw()
        x = numpy.array([x0, \
                         x0 + w * math.cos(t0), \
                         x0 + w * math.cos(t0) - h * math.sin(t0), \
                         x0 - h * math.sin(t0), \
                         x0])
        y = numpy.array([y0, \
                         y0 + w * math.sin(t0), \
                         y0 + w * math.sin(t0) + h * math.cos(t0), \
                         y0 + h * math.cos(t0), \
                         y0])
        plt.plot(x, y, 'k-')

    plt.axes().set_aspect('equal')

    plt.savefig(os.path.join(outputDir, "res_pos3_%d.png" % (num)), format='png')

def clippedStd(a, n):
    std = a.std()
    for i in range(n):
        b = a[numpy.fabs(a) < 2.0*std]
        std = b.std()

    return std

def saveResPos(matchVec, sourceVec, outputDir):
    _x = []
    _y = []
    _xbad = []
    _ybad = []
    _xm = []
    _ym = []
    f = open("dpos.dat", "wt")
    for i in range(matchVec.size()):
        if (matchVec[i].good == True):
            _x.append((matchVec[i].xi_fit - matchVec[i].xi) * 3600)
            _y.append((matchVec[i].eta_fit - matchVec[i].eta) * 3600)
            _xm.append((matchVec[i].xi_fit - matchVec[i].xi) * 3600)
            _ym.append((matchVec[i].eta_fit - matchVec[i].eta) * 3600)
            f.write("m %f %f %f %f %f %f 1\n" % (matchVec[i].xi_fit, matchVec[i].eta_fit,
                                                 matchVec[i].xi, matchVec[i].eta,
                                                 matchVec[i].u, matchVec[i].v))
        else:
            _xbad.append((matchVec[i].xi_fit - matchVec[i].xi) * 3600)
            _ybad.append((matchVec[i].eta_fit - matchVec[i].eta) * 3600)
            f.write("m %f %f %f %f %f %f 0\n" % (matchVec[i].xi_fit, matchVec[i].eta_fit,
                                                 matchVec[i].xi, matchVec[i].eta,
                                                 matchVec[i].u, matchVec[i].v))
    _xs = []
    _ys = []
    if (sourceVec != None):
        for i in range(sourceVec.size()):
            if (sourceVec[i].good == True):
                _x.append((sourceVec[i].xi_fit - sourceVec[i].xi) * 3600)
                _y.append((sourceVec[i].eta_fit - sourceVec[i].eta) * 3600)
                _xs.append((sourceVec[i].xi_fit - sourceVec[i].xi) * 3600)
                _ys.append((sourceVec[i].eta_fit - sourceVec[i].eta) * 3600)
                f.write("s %f %f %f %f %f %f 1\n" % (sourceVec[i].xi_fit, sourceVec[i].eta_fit,
                                                     sourceVec[i].xi, sourceVec[i].eta,
                                                     sourceVec[i].u, sourceVec[i].v))
            else:
                _xbad.append((sourceVec[i].xi_fit - sourceVec[i].xi) * 3600)
                _ybad.append((sourceVec[i].eta_fit - sourceVec[i].eta) * 3600)
                f.write("s %f %f %f %f %f %f 0\n" % (sourceVec[i].xi_fit, sourceVec[i].eta_fit,
                                                     sourceVec[i].xi, sourceVec[i].eta,
                                                     sourceVec[i].u, sourceVec[i].v))
    f.close()

    d_xi = numpy.array(_x)
    d_eta = numpy.array(_y)
    d_xi_bad = numpy.array(_xbad)
    d_eta_bad = numpy.array(_ybad)
    d_xi_m = numpy.array(_xm)
    d_eta_m = numpy.array(_ym)
    d_xi_s = numpy.array(_xs)
    d_eta_s = numpy.array(_ys)

    xi_std  = clippedStd(d_xi, 3)
    eta_std = clippedStd(d_eta, 3)
    xi_std_m  = clippedStd(d_xi_m, 3)
    eta_std_m = clippedStd(d_eta_m, 3)
    xi_std_s  = clippedStd(d_xi_s, 3)
    eta_std_s = clippedStd(d_eta_s, 3)

    plt.clf()
    plt.rc('text', usetex=True)

    plt.subplot2grid((5,6),(1,0), colspan=4, rowspan=4)
    plt.plot(d_xi_bad, d_eta_bad, 'k,', markeredgewidth=0)
    plt.plot(d_xi_m, d_eta_m, 'g,', markeredgewidth=0)
    plt.plot(d_xi_s, d_eta_s, 'r,', markeredgewidth=0)
    plt.xlim(-0.5, 0.5)
    plt.ylim(-0.5, 0.5)

    plt.xlabel(r'$\Delta\xi$ (arcsec)')
    plt.ylabel(r'$\Delta\eta$ (arcsec)')

    ax = plt.subplot2grid((5,6),(0,0), colspan=4)
    if sourceVec != None:
        plt.hist([d_xi, d_xi_m, d_xi_s], bins=100, normed=False, histtype='step')
    else:
        plt.hist([d_xi, d_xi_m], bins=100, normed=False, histtype='step')
    plt.text(0.75, 0.7, r"$\sigma=$%5.3f" % (xi_std), transform=ax.transAxes, color='blue')
    plt.text(0.75, 0.5, r"$\sigma=$%5.3f" % (xi_std_m), transform=ax.transAxes, color='green')
    if sourceVec != None:
        plt.text(0.75, 0.3, r"$\sigma=$%5.3f" % (xi_std_s), transform=ax.transAxes, color='red')
    plt.xlim(-0.5, 0.5)

    ax = plt.subplot2grid((5,6),(1,4), rowspan=4)
    n, bins, patches = plt.hist(d_eta, bins=100, normed=False, orientation='horizontal', histtype='step')
    plt.hist(d_eta_m, bins=bins, normed=False, orientation='horizontal', histtype='step')
    if sourceVec != None:
        plt.hist(d_eta_s, bins=bins, normed=False, orientation='horizontal', histtype='step')
    plt.text(0.7, 0.25, r"$\sigma=$%5.3f" % (eta_std), rotation=270, transform=ax.transAxes, color='blue')
    plt.text(0.5, 0.25, r"$\sigma=$%5.3f" % (eta_std_m), rotation=270, transform=ax.transAxes, color='green')
    if sourceVec != None:
        plt.text(0.3, 0.25, r"$\sigma=$%5.3f" % (eta_std_s), rotation=270, transform=ax.transAxes, color='red')
    plt.xticks(rotation=270)
    plt.yticks(rotation=270)
    plt.ylim(-0.5, 0.5)

    plt.savefig(os.path.join(outputDir, "res_pos.png"), format='png')

def saveResPos2(matchVec, sourceVec, outputDir):
    _xi = []
    _eta = []
    _x = []
    _y = []
    for i in range(matchVec.size()):
        if (matchVec[i].good == True):
            _x.append((matchVec[i].xi_fit - matchVec[i].xi) * 3600)
            _y.append((matchVec[i].eta_fit - matchVec[i].eta) * 3600)
            _xi.append(matchVec[i].xi * 3600)
            _eta.append(matchVec[i].eta * 3600)
    if (sourceVec != None):
        for i in range(sourceVec.size()):
            if (sourceVec[i].good == True):
                _x.append((sourceVec[i].xi_fit - sourceVec[i].xi) * 3600)
                _y.append((sourceVec[i].eta_fit - sourceVec[i].eta) * 3600)
                _xi.append(sourceVec[i].xi * 3600)
                _eta.append(sourceVec[i].eta * 3600)

    xi = numpy.array(_xi)
    eta = numpy.array(_eta)
    d_xi = numpy.array(_x)
    d_eta = numpy.array(_y)

    plt.clf()
    plt.rc('text', usetex=True)

    plt.subplot(2, 2, 1)
    plt.plot(xi, d_xi, ',', markeredgewidth=0)
    plt.xlabel(r'$\xi$ (arcsec)')
    plt.ylabel(r'$\Delta\xi$ (arcsec)')

    plt.subplot(2, 2, 3)
    plt.plot(xi, d_eta, ',', markeredgewidth=0)
    plt.xlabel(r'$\xi$ (arcsec)')
    plt.ylabel(r'$\Delta\eta$ (arcsec)')

    plt.subplot(2, 2, 2)
    plt.plot(eta, d_xi, ',', markeredgewidth=0)
    plt.xlabel(r'$\eta$ (arcsec)')
    plt.ylabel(r'$\Delta\xi$ (arcsec)')

    plt.subplot(2, 2, 4)
    plt.plot(eta, d_xi, ',', markeredgewidth=0)
    plt.xlabel(r'$\eta$ (arcsec)')
    plt.ylabel(r'$\Delta\eta$ (arcsec)')

    plt.savefig(os.path.join(outputDir, "res_pos2.png"), format='png')

def saveResFlux(matchVec, fexp, fchip, nexp, ccdSet, ffp, outputDir):
    _dmag = []
    _iexp = []
    _ichip = []
    _r = []
    for i in range(matchVec.size()):
        if (matchVec[i].good == True and matchVec[i].mag != -9999 and matchVec[i].jstar != -1):
            mag = matchVec[i].mag
            mag0 = matchVec[i].mag0
            exp_cor = -2.5 * math.log10(fexp[matchVec[i].iexp])
            chip_cor = -2.5 * math.log10(fchip[matchVec[i].ichip])
            gain_cor = ffp.eval(matchVec[i].u, matchVec[i].v)
            mag_cor = mag + exp_cor + chip_cor + gain_cor
            diff = mag_cor - mag0
            _dmag.append(diff)
            _iexp.append(matchVec[i].iexp)
            _ichip.append(matchVec[i].ichip)

    d_mag = numpy.array(_dmag)
    iexp = numpy.array(_iexp)
    ichip = numpy.array(_ichip)

    mag_std  = clippedStd(d_mag, 3)

    _r = []
    _dm = []
    for ccd in ccdSet.values():
        w = ccd.getAllPixels(True).getWidth()
        h = ccd.getAllPixels(True).getHeight()
        _x0 = ccd.getCenter().getPixels(ccd.getPixelSize())[0] + 0.5 * w
        _y0 = ccd.getCenter().getPixels(ccd.getPixelSize())[1] + 0.5 * h
        _r.append(math.sqrt(_x0*_x0 + _y0*_y0))
        _dm.append(-2.5 * math.log10(fchip[ccd.getId().getSerial()]))

    r = numpy.array(_r)
    dm = numpy.array(_dm)

    plt.clf()
    plt.rc('text', usetex=True)

    ax = plt.subplot(2, 2, 1)
    plt.hist(d_mag, bins=100, normed=True, histtype='step')
    plt.text(0.1, 0.7, r"$\sigma=$%7.5f" % (mag_std), transform=ax.transAxes)
    plt.xlabel(r'$\Delta mag$ (mag)')

    ax = plt.subplot(2, 2, 2)
    plt.plot(r, dm, 'o')
    plt.xlabel(r'Distance from center (pixel)')
    plt.ylabel(r'Offset in magnitude')

    ax = plt.subplot(2, 2, 3)
    plt.plot(iexp, d_mag, ',', markeredgewidth=0)
    plt.xlabel(r'Exposure ID')
    plt.ylabel(r'$\Delta mag$ (mag)')
    plt.xlim(iexp.min()-1, iexp.max()+1)
    plt.ylim(-0.2, 0.2)

    ax = plt.subplot(2, 2, 4)
    plt.plot(ichip, d_mag, ',', markeredgewidth=0)
    plt.xlabel(r'Chip ID')
    plt.ylabel(r'$\Delta mag$ (mag)')
    plt.xlim(ichip.min()-1, ichip.max()+1)
    plt.ylim(-0.2, 0.2)

    plt.savefig(os.path.join(outputDir, "res_flux.png"), format='png')

def saveResFlux0(matchVec, sourceVec, fexp, fchip, nexp, ccdSet, ffp, outputDir):
    _dmag_m = []
    _dmag_s = []
    _dmag_a = []
    _dmag_bad = []
    _mag0_m = []
    _mag0_s = []
    _mag0_bad = []
    f = open('dmag.dat', 'wt')
    for i in range(matchVec.size()):
        if (matchVec[i].good == True and matchVec[i].mag != -9999 and matchVec[i].jstar != -1 and matchVec[i].mag0 != -9999):
            mag = matchVec[i].mag
            mag0 = matchVec[i].mag0
            mag_cat = matchVec[i].mag_cat
            exp_cor = -2.5 * math.log10(fexp[matchVec[i].iexp])
            chip_cor = -2.5 * math.log10(fchip[matchVec[i].ichip])
            gain_cor = ffp.eval(matchVec[i].u, matchVec[i].v)
            mag_cor = mag + exp_cor + chip_cor + gain_cor
            diff = mag_cor - mag0
            _dmag_m.append(diff)
            _dmag_a.append(diff)
            _mag0_m.append(mag0)
            f.write("m %f %f %f %f %f 1\n" % (mag_cor, mag0, mag_cat,
                                              matchVec[i].u, matchVec[i].v))
        else:
            mag = matchVec[i].mag
            mag0 = matchVec[i].mag0
            mag_cat = matchVec[i].mag_cat
            exp_cor = -2.5 * math.log10(fexp[matchVec[i].iexp])
            chip_cor = -2.5 * math.log10(fchip[matchVec[i].ichip])
            gain_cor = ffp.eval(matchVec[i].u, matchVec[i].v)
            mag_cor = mag + exp_cor + chip_cor + gain_cor
            diff = mag_cor - mag0
            _dmag_bad.append(diff)
            _mag0_bad.append(mag0)
            f.write("m %f %f %f %f %f 0\n" % (mag_cor, mag0, mag_cat,
                                              matchVec[i].u, matchVec[i].v))
    if sourceVec != None:
        for i in range(sourceVec.size()):
            if (sourceVec[i].good == True and sourceVec[i].mag != -9999 and sourceVec[i].jstar != -1):
                mag = sourceVec[i].mag
                mag0 = sourceVec[i].mag0
                exp_cor = -2.5 * math.log10(fexp[sourceVec[i].iexp])
                chip_cor = -2.5 * math.log10(fchip[sourceVec[i].ichip])
                gain_cor = ffp.eval(sourceVec[i].u, sourceVec[i].v)
                mag_cor = mag + exp_cor + chip_cor + gain_cor
                diff = mag_cor - mag0
                _dmag_s.append(diff)
                _dmag_a.append(diff)
                _mag0_s.append(mag0)
                f.write("s %f %f %f %f %f 1\n" % (mag_cor, mag0, -9999,
                                                  sourceVec[i].u, sourceVec[i].v))
            else:
                mag = sourceVec[i].mag
                mag0 = sourceVec[i].mag0
                exp_cor = -2.5 * math.log10(fexp[sourceVec[i].iexp])
                chip_cor = -2.5 * math.log10(fchip[sourceVec[i].ichip])
                gain_cor = ffp.eval(sourceVec[i].u, sourceVec[i].v)
                mag_cor = mag + exp_cor + chip_cor + gain_cor
                diff = mag_cor - mag0
                _dmag_bad.append(diff)
                _mag0_bad.append(mag0)
                f.write("s %f %f %f %f %f 0\n" % (mag_cor, mag0, -9999,
                                                  sourceVec[i].u, sourceVec[i].v))
    f.close()

    d_mag_m = numpy.array(_dmag_m)
    d_mag_s = numpy.array(_dmag_s)
    d_mag_a = numpy.array(_dmag_a)
    d_mag_bad = numpy.array(_dmag_bad)
    mag0_m = numpy.array(_mag0_m)
    mag0_s = numpy.array(_mag0_s)
    mag0_bad = numpy.array(_mag0_bad)

    mag_std_m  = clippedStd(d_mag_m, 3)
    mag_std_s  = clippedStd(d_mag_s, 3)
    mag_std_a  = clippedStd(d_mag_a, 3)

    plt.clf()
    plt.rc('text', usetex=True)

    plt.subplot2grid((5,6),(1,0), colspan=4, rowspan=4)
    plt.plot(mag0_bad, d_mag_bad, 'k,', markeredgewidth=0)
    if sourceVec != None:
        plt.plot(mag0_s, d_mag_s, 'r,', markeredgewidth=0)
    plt.plot(mag0_m, d_mag_m, 'g,', markeredgewidth=0)
    plt.plot([15,25], [0,0], 'k--')
    plt.xlim(15, 25)
    plt.ylim(-0.25, 0.25)

    ax = plt.subplot2grid((5,6),(1,4), rowspan=4)
    n, bins, patches = plt.hist(d_mag_a, bins=100, normed=False, orientation='horizontal', histtype='step')
    plt.hist(d_mag_m, bins=bins, normed=False, orientation='horizontal', histtype='step')
    if sourceVec != None:
        plt.hist(d_mag_s, bins=bins, normed=False, orientation='horizontal', histtype='step')
    plt.text(0.7, 0.25, r"$\sigma=$%5.3f" % (mag_std_a), rotation=270, transform=ax.transAxes, color='blue')
    plt.text(0.5, 0.25, r"$\sigma=$%5.3f" % (mag_std_m), rotation=270, transform=ax.transAxes, color='green')
    if sourceVec != None:
        plt.text(0.3, 0.25, r"$\sigma=$%5.3f" % (mag_std_s), rotation=270, transform=ax.transAxes, color='red')
    plt.xticks(rotation=270)
    plt.yticks(rotation=270)
    plt.ylim(-0.25, 0.25)

    #plt.xlabel(r'$\Delta mag$ (mag)')

    plt.savefig(os.path.join(outputDir, "res_flux0.png"), format='png')

def saveResFlux2(matchVec, fexp, fchip, nexp, ccdSet, ffp, outputDir):
    _dmag = []
    _u = []
    _v = []
    for i in range(matchVec.size()):
        if (matchVec[i].good == True and matchVec[i].mag != -9999 and matchVec[i].jstar != -1):
            mag = matchVec[i].mag
            mag0 = matchVec[i].mag0
            exp_cor = -2.5 * math.log10(fexp[matchVec[i].iexp])
            chip_cor = -2.5 * math.log10(fchip[matchVec[i].ichip])
            gain_cor = ffp.eval(matchVec[i].u, matchVec[i].v)
            mag_cor = mag + exp_cor + chip_cor + gain_cor
            diff = mag_cor - mag0
            _dmag.append(diff)
            _u.append(matchVec[i].u)
            _v.append(matchVec[i].v)

    d_mag = numpy.array(_dmag)
    u = numpy.array(_u)
    v = numpy.array(_v)

    s = numpy.absolute(d_mag) * 10

    u1 = [u[i] for i in range(len(d_mag)) if d_mag[i] > 0]
    v1 = [v[i] for i in range(len(d_mag)) if d_mag[i] > 0]
    s1 = [math.fabs(d_mag[i])*20 for i in range(len(d_mag)) if d_mag[i] > 0]
    u2 = [u[i] for i in range(len(d_mag)) if d_mag[i] < 0]
    v2 = [v[i] for i in range(len(d_mag)) if d_mag[i] < 0]
    s2 = [math.fabs(d_mag[i])*20 for i in range(len(d_mag)) if d_mag[i] < 0]

    plt.clf()
    plt.rc('text', usetex=True)

    plt.scatter(u1, v1, s1, color='blue')
    plt.scatter(u2, v2, s2, color='red')
    plt.axes().set_aspect('equal')

    plt.savefig(os.path.join(outputDir, "res_flux2.png"), format='png')

def outputDiag(matchVec, sourceVec, coeffSet, ccdSet, fexp, fchip, ffp, outputDir="."):
    print "Output Diagnostic Figures..."

    f = open(os.path.join(outputDir, "coeffs.dat"), "wt")
    for iexp in coeffSet.keys():
        f.write("%ld %12.5e %12.5e\n" % (iexp, coeffSet[iexp].A, coeffSet[iexp].D));
        f.write("%ld %12.5f %12.5f\n" % (iexp, coeffSet[iexp].x0, coeffSet[iexp].y0));
        for k in range(coeffSet[iexp].getNcoeff()):
            f.write("%ld %15.8e %15.8e %15.8e %15.8e\n" % (iexp, coeffSet[iexp].get_a(k), coeffSet[iexp].get_b(k), coeffSet[iexp].get_ap(k), coeffSet[iexp].get_bp(k)));
        f.write("%5.3f\n" % (-2.5*math.log10(fexp[iexp])))
    f.close()

    f = open(os.path.join(outputDir, "ccd.dat"), "wt")
    for ichip in ccdSet.keys():
        center = ccdSet[ichip].getCenter().getPixels(ccdSet[ichip].getPixelSize())
        orient = ccdSet[ichip].getOrientation()
        f.write("%3ld %10.3f %10.3f %10.7f %5.3f\n" % (ichip, center[0], center[1], orient.getYaw(), fchip[ichip]));
    f.close()

    #for iexp in coeffSet.keys():
    #    plotJCont(iexp, coeffSet[iexp], ccdSet, outputDir=outputDir)
    #    plotFCorCont(iexp, coeffSet[iexp], ccdSet, ffp, outputDir)
    #    saveResPos3(matchVec, sourceVec, iexp, coeffSet[iexp], ccdSet, outputDir)

    saveResPos(matchVec, sourceVec, outputDir)
    #saveResPos2(matchVec, sourceVec, outputDir)
    #saveResFlux(matchVec, fexp, fchip, coeffSet.size(), ccdSet, ffp, outputDir)
    #saveResFlux2(matchVec, fexp, fchip, coeffSet.size(), ccdSet, ffp, outputDir)
    saveResFlux0(matchVec, sourceVec, fexp, fchip, coeffSet.size(), ccdSet, ffp, outputDir)

    print datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")

def getExtent(matchVec):
    u_max = float("-inf")
    v_max = float("-inf")
    for m in matchVec:
        if (math.fabs(m.u) > u_max):
            u_max = math.fabs(m.u)
        if (math.fabs(m.v) > v_max):
            v_max = math.fabs(m.v)

    return u_max, v_max

def checkInputs(wcsDic, sourceSet, matchList):
    newWcsDic = measMosaic.WcsDic()
    newSourceSet = measMosaic.SourceGroup()
    newMatchList = measMosaic.SourceMatchGroup()
    for i, (wcs, frame, ss, ml) in enumerate(zip(wcsDic.values(), wcsDic.keys(), sourceSet, matchList)):
        if len(ss) > 0 or len(ml) > 0:
            newWcsDic[frame] = wcs
            newSourceSet.push_back(ss)
            newMatchList.push_back(ml)
    return newWcsDic, newSourceSet, newMatchList


def mosaic(butler, frameIds, ccdIds, ct=None, config=measMosaicConfig.MosaicConfig(),
           outputDir=".", debug=False, verbose=False):

    ccdSet = readCcd(butler.mapper.camera, ccdIds)
    mem = int(os.popen('/bin/ps -o vsz %d' % os.getpid()).readlines()[-1])
    print "(Memory) After readCcd : ", mem

    if debug:
        for ccd in ccdSet.values():
            print (ccd.getId().getSerial(), ccd.getCenter().getPixels(ccd.getPixelSize()),
                   ccd.getOrientation().getYaw())

    wcsDic = readWcs(butler, frameIds, ccdSet)
    print wcsDic.keys()
    mem = int(os.popen('/bin/ps -o vsz %d' % os.getpid()).readlines()[-1])
    print "(Memory) After readWcs : ", mem

    removeNonExistCcd(butler, ccdSet, wcsDic)
    print ccdSet.keys()

    if debug:
        for iexp, wcs in wcsDic.iteritems():
            print iexp, wcs.getPixelOrigin(), wcs.getSkyOrigin().getPosition(afwGeom.degrees)

    sourceSet, matchList = readCatalog(butler, wcsDic.keys(), ccdSet.keys(), ct)
    mem = int(os.popen('/bin/ps -o vsz %d' % os.getpid()).readlines()[-1])
    print "(Memory) After readCatalog : ", mem

    wcsDic, sourceSet, matchList = checkInputs(wcsDic, sourceSet, matchList)
    print wcsDic.size(), sourceSet.size(), matchList.size()
    print wcsDic.keys()

    d_lim = afwGeom.Angle(config.radXMatch, afwGeom.arcseconds)
    nbrightest = config.nBrightest
    if verbose:
        print "d_lim : ", d_lim
        print "nbrightest : ", nbrightest
    allMat, allSource = mergeCatalog(sourceSet, matchList, ccdSet, d_lim, nbrightest)
    mem = int(os.popen('/bin/ps -o vsz %d' % os.getpid()).readlines()[-1])
    print "(Memory) After mergeCatalog : ", mem

    nmatch  = allMat.size()
    nsource = allSource.size()
    matchVec  = measMosaic.obsVecFromSourceGroup(allMat,    wcsDic, ccdSet)
    sourceVec = measMosaic.obsVecFromSourceGroup(allSource, wcsDic, ccdSet)
    mem = int(os.popen('/bin/ps -o vsz %d' % os.getpid()).readlines()[-1])
    print "(Memory) After obsVecFromSourceGroup : ", mem

    print "Solve mosaic ..."
    order = config.fittingOrder
    internal = config.internalFitting
    solveCcd = config.solveCcd
    allowRotation = config.allowRotation
    fluxFitOrder = config.fluxFitOrder
    chebyshev = config.chebyshev
    absolute = config.fluxFitAbsolute

    ffp = measMosaic.FluxFitParams(fluxFitOrder, absolute, chebyshev)
    u_max, v_max = getExtent(matchVec)
    ffp.u_max = (math.floor(u_max / 10.) + 1) * 10
    ffp.v_max = (math.floor(v_max / 10.) + 1) * 10

    if verbose:
        print "order : ", ffp.order
        print "internal : ", internal
        print "solveCcd : ", solveCcd
        print "allowRotation : ", allowRotation

    #fscale = afwMath.vectorD()
    fexp = measMosaic.map_int_float()
    fchip = measMosaic.map_int_float()
    if internal:
        coeffSet = measMosaic.solveMosaic_CCD(order, nmatch, nsource,
                                              matchVec, sourceVec,
                                              wcsDic, ccdSet, ffp, fexp, fchip,
                                              solveCcd, allowRotation, verbose)
    else:
        coeffSet = measMosaic.solveMosaic_CCD_shot(order, nmatch, matchVec, 
                                                   wcsDic, ccdSet, ffp, fexp, fchip,
                                                   solveCcd, allowRotation, verbose)
    mem = int(os.popen('/bin/ps -o vsz %d' % os.getpid()).readlines()[-1])
    print "(Memory) After solveMosaic_CCD : ", mem
    print datetime.datetime.today().strftime("%Y-%m-%d %H:%M:%S")

    writeNewWcs(butler, coeffSet, ccdSet, fexp, fchip)
    writeFcr(butler, coeffSet, ccdSet, fexp, fchip, ffp)

    if internal:
        outputDiag(matchVec, sourceVec, coeffSet, ccdSet, fexp, fchip, ffp, outputDir=outputDir)
    else:
        outputDiag(matchVec, None, coeffSet, ccdSet, fexp, fchip, ffp, outputDir=outputDir)

    #writeDetJImg(butler, coeffSet, ccdSet, frameIds, ccdIds)
    #writeDCorImg(butler, coeffSet, ccdSet, frameIds, ccdIds, ffp)

    return wcsDic.keys()
